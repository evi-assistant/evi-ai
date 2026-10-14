"""Integrity of what the managed runtime downloads and runs.

- Every llama.cpp release asset and catalog GGUF carries a pinned sha256; a
  download that doesn't match (or has no pin) is discarded before it is
  extracted, chmod'ed or run, and leaves no file behind.
- Archives are unpacked with path checks: a `../x`, absolute or link-escaping
  member writes nothing, inside or outside the target.
- The macOS toast passes its text to osascript as argv, never as AppleScript.
- scripts/pin_runtime_hashes.py regenerates the pins and checks them.

No network: urlopen is faked everywhere a download would happen.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import json
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

import pytest

from evi import notify
from evi.runtime import catalog as cat
from evi.runtime import llamacpp_runtime as rt

ROOT = Path(__file__).resolve().parents[1]


def _load_pin_script():
    path = ROOT / "scripts" / "pin_runtime_hashes.py"
    spec = importlib.util.spec_from_file_location("pin_runtime_hashes", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


pin = _load_pin_script()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Resp(io.BytesIO):
    """A urlopen() response: a byte stream with headers."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}


def _serve(monkeypatch, blobs: dict[str, bytes]) -> list[str]:
    """Fake urlopen: serve `blobs` by URL basename. Returns the requested URLs."""
    seen: list[str] = []

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        seen.append(url)
        return _Resp(blobs[url.rsplit("/", 1)[-1]])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return seen


def _no_network(monkeypatch) -> None:
    def boom(*a, **k):
        raise AssertionError("nothing may be fetched without a pin")

    monkeypatch.setattr(urllib.request, "urlopen", boom)


def _files_under(path: Path) -> list[Path]:
    return [p for p in path.rglob("*") if not p.is_dir()] if path.exists() else []


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def _tar_bytes(members: list[tarfile.TarInfo | tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for m in members:
            if isinstance(m, tarfile.TarInfo):
                t.addfile(m)
            else:
                name, data = m
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o755
                t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _link(name: str, target: str, kind: bytes = tarfile.SYMTYPE) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = target
    return info


def _fake_rt(**attrs):
    """A stand-in runtime module for the pin script, its asset list derived the
    way the real module derives it (every `_*_ASSETS` table)."""
    attrs.setdefault("_ASSET_SHA256", {})
    return type("RT", (), {**attrs,
                           "release_assets": staticmethod(lambda: rt.asset_names(attrs))})


_REPIN = "run `python scripts/pin_runtime_hashes.py` and commit the result"


# ---- pins ------------------------------------------------------------------


def test_every_runtime_asset_and_catalog_model_is_pinned():
    """The CI guard: bumping LLAMACPP_VERSION or adding a model without
    re-running scripts/pin_runtime_hashes.py fails here."""
    assets = pin.runtime_assets(rt)
    cuda = {a for builds in rt._CUDA_ASSETS.values() for pair in builds.values() for a in pair}
    assert set(assets) == rt.release_assets() >= set(rt._CPU_ASSETS.values()) | cuda
    problems = pin.check_pins(assets, rt._ASSET_SHA256, cat.catalog())
    assert problems == [], f"unpinned downloads; {_REPIN}: {problems}"
    for asset in assets:
        assert len(rt.asset_sha256(asset)) == 64


def test_a_new_asset_table_is_pinned_and_checked_automatically(monkeypatch):
    """One source of truth: a third table (a Vulkan build, say) is enumerated by
    the script and flagged by the guard without anyone registering it."""
    vulkan = f"llama-{rt.LLAMACPP_VERSION}-bin-win-vulkan-x64.zip"
    monkeypatch.setattr(rt, "_VULKAN_ASSETS", {"x86_64": vulkan}, raising=False)
    assert vulkan in rt.release_assets()
    assert vulkan in pin.runtime_assets(rt)
    problems = pin.check_pins(pin.runtime_assets(rt), rt._ASSET_SHA256, [])
    assert problems == [f"runtime asset {vulkan}: no valid sha256 pin"]
    # ...and a pin for it is not reported stale (so a re-pin wouldn't drop it).
    monkeypatch.setitem(rt._ASSET_SHA256, vulkan, "a" * 64)
    assert pin.check_pins(pin.runtime_assets(rt), rt._ASSET_SHA256, []) == []
    with pytest.raises(rt.IntegrityError, match="No pinned sha256"):
        rt.asset_sha256("llama-unlisted.zip")


def test_catalog_downloads_from_the_pinned_commit():
    for entry in cat.catalog():
        url = cat.download_url(entry)
        assert f"/resolve/{entry['revision']}/{entry['filename']}" in url
        assert "/resolve/main/" not in url


def test_generated_block_matches_the_source():
    """The pins in llamacpp_runtime.py are exactly what the script renders, so a
    re-run on an unchanged release is a no-op."""
    source = (ROOT / "evi" / "runtime" / "llamacpp_runtime.py").read_text(encoding="utf-8")
    assert pin.render_block(rt._ASSET_SHA256) in source
    assert pin.replace_block(source, dict(rt._ASSET_SHA256)) == source


def test_version_bump_without_repin_is_caught():
    fake = _fake_rt(_CPU_ASSETS={("linux", "x86_64"): "llama-b99999-bin-ubuntu-x64.tar.gz"},
                    _CUDA_ASSETS={})
    stale = {"llama-b10453-bin-ubuntu-x64.tar.gz": "a" * 64}
    problems = pin.check_pins(pin.runtime_assets(fake), stale, [])
    assert any("b99999" in p and "no valid sha256" in p for p in problems)
    assert any("b10453" in p and "stale" in p for p in problems)


def test_check_flags_missing_and_malformed_model_pins():
    models = [
        {"id": "ok", "revision": "0" * 40, "sha256": "f" * 64},
        {"id": "nosha", "revision": "0" * 40},
        {"id": "short", "revision": "main", "sha256": "abc"},
    ]
    problems = pin.check_pins([], {}, models)
    assert not any("model ok" in p for p in problems)
    assert any("nosha" in p and "sha256" in p for p in problems)
    assert any("short" in p and "sha256" in p for p in problems)
    assert any("short" in p and "revision" in p for p in problems)


# ---- download_file -----------------------------------------------------------


def test_download_file_matching_pin_lands(monkeypatch, tmp_path):
    data = b"verified bytes" * 1000
    _serve(monkeypatch, {"blob.bin": data})
    seen: list[tuple[int, int]] = []
    dest = tmp_path / "out" / "blob.bin"
    # A "sha256:"-prefixed, upper-case pin is the same pin.
    out = rt.download_file("https://example.invalid/blob.bin", dest,
                           lambda d, t: seen.append((d, t)),
                           sha256="sha256:" + _sha(data).upper())
    assert out == dest and dest.read_bytes() == data
    assert [p.name for p in _files_under(tmp_path)] == ["blob.bin"]   # no .part left
    assert seen[-1] == (len(data), len(data))


def test_download_file_mismatch_leaves_nothing(monkeypatch, tmp_path):
    _serve(monkeypatch, {"blob.bin": b"tampered"})
    dest = tmp_path / "blob.bin"
    with pytest.raises(rt.IntegrityError, match="failed verification"):
        rt.download_file("https://example.invalid/blob.bin", dest, sha256=_sha(b"original"))
    assert _files_under(tmp_path) == []


def test_download_file_failed_transfer_leaves_nothing(monkeypatch, tmp_path):
    class Broken(_Resp):
        def read(self, n=-1):
            if self.tell():
                raise ConnectionResetError("dropped")
            return super().read(4)

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: Broken(b"x" * 64))
    with pytest.raises(ConnectionResetError):
        rt.download_file("https://example.invalid/b", tmp_path / "b", sha256="0" * 64)
    assert _files_under(tmp_path) == []


@pytest.mark.parametrize("pin_value", ["", None, "main", "z" * 64, "ab" * 31])
def test_download_file_refuses_without_a_valid_pin(monkeypatch, tmp_path, pin_value):
    _no_network(monkeypatch)
    with pytest.raises(rt.IntegrityError, match="No pinned sha256"):
        rt.download_file("https://example.invalid/b", tmp_path / "b", sha256=pin_value)
    assert _files_under(tmp_path) == []


def test_download_file_cleanup_failure_never_masks_the_integrity_error(monkeypatch, tmp_path):
    """An AV scanner / indexer holding the just-closed .part (WinError 32) must
    not turn 'failed verification' into a PermissionError."""
    _serve(monkeypatch, {"blob.bin": b"tampered"})
    real_unlink = Path.unlink

    def locked(self, *a, **k):
        if self.name.endswith(".part"):
            raise PermissionError(32, "The process cannot access the file")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", locked)
    with pytest.raises(rt.IntegrityError, match="failed verification"):
        rt.download_file("https://example.invalid/blob.bin", tmp_path / "blob.bin",
                         sha256=_sha(b"original"))
    assert not (tmp_path / "blob.bin").exists()


def test_concurrent_downloads_of_one_dest_never_share_bytes(monkeypatch, tmp_path):
    """A second download of the same dest (the desktop sidecar and the CLI are
    separate processes) starting mid-stream gets its own sidecar: the tampered
    one fails on its own, and the genuine one lands exactly as hashed."""
    genuine = b"G" * (3 << 20)
    dest = tmp_path / "model.gguf"
    errors: list[BaseException] = []

    class Genuine(_Resp):
        chunks = 0

        def read(self, n=-1):
            Genuine.chunks += 1
            if Genuine.chunks == 2:  # the other download runs in the middle of ours
                monkeypatch.setattr(urllib.request, "urlopen",
                                    lambda req, timeout=None: _Resp(b"T" * (2 << 20)))
                try:
                    rt.download_file("https://example.invalid/model.gguf", dest,
                                     sha256=_sha(genuine))
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)
            return super().read(n)

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: Genuine(genuine))
    rt.download_file("https://example.invalid/model.gguf", dest, sha256=_sha(genuine))
    assert len(errors) == 1 and isinstance(errors[0], rt.IntegrityError)
    assert dest.read_bytes() == genuine
    assert [p.name for p in _files_under(tmp_path)] == ["model.gguf"]


def test_download_file_sweeps_only_abandoned_sidecars(monkeypatch, tmp_path):
    import os
    import time

    old = time.time() - 2 * 3600
    stale = [tmp_path / "m.gguf.part", tmp_path / "m.gguf.0123abcd.part"]
    live = tmp_path / "m.gguf.89abcdef.part"          # another download, still writing
    other = tmp_path / "m.gguf-other.part"            # not ours
    for p in [*stale, live, other]:
        p.write_bytes(b"partial")
    for p in [*stale, other]:
        os.utime(p, (old, old))
    data = b"weights"
    _serve(monkeypatch, {"m.gguf": data})
    rt.download_file("https://example.invalid/m.gguf", tmp_path / "m.gguf", sha256=_sha(data))
    assert sorted(p.name for p in _files_under(tmp_path)) == sorted(
        ["m.gguf", live.name, other.name])


# ---- ensure_runtime / ensure_gpu_runtime -------------------------------------


@pytest.fixture
def linux_x64(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(rt.platform, "system", lambda: "Linux")
    monkeypatch.setattr(rt.platform, "machine", lambda: "x86_64")
    chmods: list[Path] = []
    monkeypatch.setattr(Path, "chmod", lambda self, mode, **k: chmods.append(self))
    return chmods


@pytest.fixture
def windows_x64(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(rt.platform, "system", lambda: "Windows")
    monkeypatch.setattr(rt.platform, "machine", lambda: "AMD64")


def test_tampered_runtime_archive_rejected_before_chmod(monkeypatch, tmp_path, linux_x64):
    asset = rt.cpu_asset()
    good = _tar_bytes([("llama-b/llama-server", b"#!/bin/sh\necho genuine\n")])
    evil = _tar_bytes([("llama-b/llama-server", b"#!/bin/sh\necho owned\n")])
    monkeypatch.setitem(rt._ASSET_SHA256, asset, _sha(good))
    _serve(monkeypatch, {asset: evil})

    with pytest.raises(rt.IntegrityError):
        rt.ensure_runtime()
    assert linux_x64 == []                      # never chmod'ed
    assert rt.server_path() is None             # nothing to exec
    assert _files_under(tmp_path) == []         # no archive, .part or binary


def test_genuine_runtime_archive_installs(monkeypatch, tmp_path, linux_x64):
    asset = rt.cpu_asset()
    good = _tar_bytes([("llama-b/llama-server", b"#!/bin/sh\necho genuine\n")])
    monkeypatch.setitem(rt._ASSET_SHA256, asset, _sha(good))
    _serve(monkeypatch, {asset: good})

    server = rt.ensure_runtime()
    assert server.read_bytes() == b"#!/bin/sh\necho genuine\n"
    assert linux_x64 == [server]
    assert not (rt.runtime_root() / asset).exists()   # archive reclaimed


def test_tampered_windows_zip_rejected(monkeypatch, tmp_path, windows_x64):
    asset = rt.cpu_asset()
    good = _zip_bytes({"llama-server.exe": b"MZ genuine"})
    monkeypatch.setitem(rt._ASSET_SHA256, asset, _sha(good))
    _serve(monkeypatch, {asset: _zip_bytes({"llama-server.exe": b"MZ owned"})})
    with pytest.raises(rt.IntegrityError):
        rt.ensure_runtime()
    assert rt.server_path() is None and _files_under(tmp_path) == []


def test_unpinned_runtime_asset_is_never_fetched(monkeypatch, tmp_path, linux_x64):
    monkeypatch.delitem(rt._ASSET_SHA256, rt.cpu_asset())
    _no_network(monkeypatch)
    with pytest.raises(rt.IntegrityError, match="No pinned sha256"):
        rt.ensure_runtime()
    assert _files_under(tmp_path) == []


def test_gpu_runtime_extracts_nothing_unless_both_archives_verify(
        monkeypatch, tmp_path, windows_x64):
    llama_zip, cudart_zip = rt.gpu_plan(compute_cap="8.9")["assets"]
    llama = _zip_bytes({"llama-server.exe": b"MZ cuda build"})
    cudart = _zip_bytes({"cudart64_12.dll": b"genuine dll"})
    monkeypatch.setitem(rt._ASSET_SHA256, llama_zip, _sha(llama))
    monkeypatch.setitem(rt._ASSET_SHA256, cudart_zip, _sha(cudart))
    _serve(monkeypatch, {llama_zip: llama, cudart_zip: _zip_bytes({"cudart64_12.dll": b"evil"})})

    with pytest.raises(rt.IntegrityError):
        rt.ensure_gpu_runtime("8.9")
    assert rt.gpu_server_path() is None
    assert _files_under(tmp_path) == []

    _serve(monkeypatch, {llama_zip: llama, cudart_zip: cudart})
    server = rt.ensure_gpu_runtime("8.9")
    assert server.read_bytes() == b"MZ cuda build"
    assert sorted(p.name for p in _files_under(rt.gpu_root())) == [
        "cudart64_12.dll", "llama-server.exe"]


def _fail_extracting(monkeypatch, archive_name: str):
    """Make extracting `archive_name` die midway with ENOSPC, after its first
    member is already written (a full disk, an AV lock). Returns the undo."""
    real = zipfile.ZipFile._extract_member

    def extract_member(self, *a, **k):
        if Path(self.filename).name == archive_name and getattr(self, "_wrote", False):
            raise OSError(28, "No space left on device")
        self._wrote = True
        return real(self, *a, **k)

    monkeypatch.setattr(zipfile.ZipFile, "_extract_member", extract_member)
    return lambda: monkeypatch.setattr(zipfile.ZipFile, "_extract_member", real)


def test_gpu_extraction_failure_leaves_no_half_install(monkeypatch, tmp_path, windows_x64):
    """Both zips verify, then unpacking the cudart one fails: gpu_installed()
    must not report a llama-server.exe that has no CUDA DLLs beside it."""
    llama_zip, cudart_zip = rt.gpu_plan(compute_cap="8.9")["assets"]
    llama = _zip_bytes({"llama-server.exe": b"MZ cuda build", "ggml-cuda.dll": b"g"})
    cudart = _zip_bytes({"cudart64_12.dll": b"rt", "cublas64_12.dll": b"bl"})
    monkeypatch.setitem(rt._ASSET_SHA256, llama_zip, _sha(llama))
    monkeypatch.setitem(rt._ASSET_SHA256, cudart_zip, _sha(cudart))
    _serve(monkeypatch, {llama_zip: llama, cudart_zip: cudart})
    disk_freed = _fail_extracting(monkeypatch, cudart_zip)

    with pytest.raises(OSError, match="No space"):
        rt.ensure_gpu_runtime("8.9")
    assert not rt.gpu_installed()
    assert _files_under(tmp_path) == []          # no staging, archive or DLL left

    disk_freed()                                  # a retry installs it all
    server = rt.ensure_gpu_runtime("8.9")
    assert sorted(p.name for p in server.parent.iterdir()) == [
        "cublas64_12.dll", "cudart64_12.dll", "ggml-cuda.dll", "llama-server.exe"]


def test_cpu_extraction_failure_leaves_no_half_install(monkeypatch, tmp_path, windows_x64):
    """llama-server.exe unpacks first, then a DLL fails to write: the runtime
    must not look installed with its DLLs missing."""
    asset = rt.cpu_asset()
    good = _zip_bytes({"llama-server.exe": b"MZ", "ggml.dll": b"dll", "llama.dll": b"l"})
    monkeypatch.setitem(rt._ASSET_SHA256, asset, _sha(good))
    _serve(monkeypatch, {asset: good})
    _fail_extracting(monkeypatch, asset)
    with pytest.raises(OSError):
        rt.ensure_runtime()
    assert rt.server_path() is None
    assert _files_under(tmp_path) == []


def test_publish_moves_the_server_last(monkeypatch, tmp_path, windows_x64):
    """If moving into place fails partway, the server is never the part that
    already moved: it only appears once its libraries are beside it."""
    asset = rt.cpu_asset()
    # Alphabetically the server would move first; it must move last.
    good = _zip_bytes({"llama-server.exe": b"MZ", "zz-ggml.dll": b"dll"})
    monkeypatch.setitem(rt._ASSET_SHA256, asset, _sha(good))
    _serve(monkeypatch, {asset: good})
    import os

    real_replace = os.replace

    def replace(src, dst, *a, **k):
        if Path(dst).name == "zz-ggml.dll":
            raise PermissionError(32, "locked")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(rt.os, "replace", replace)
    with pytest.raises(PermissionError):
        rt.ensure_runtime()
    assert rt.server_path() is None


def test_cpu_archive_never_replaces_the_gpu_build(monkeypatch, tmp_path, windows_x64):
    (rt.gpu_root()).mkdir(parents=True)
    (rt.gpu_root() / "llama-server.exe").write_bytes(b"MZ cuda")
    asset = rt.cpu_asset()
    evil = _zip_bytes({"gpu/llama-server.exe": b"MZ other", "llama-server.exe": b"MZ"})
    monkeypatch.setitem(rt._ASSET_SHA256, asset, _sha(evil))
    _serve(monkeypatch, {asset: evil})
    with pytest.raises(rt.IntegrityError, match="gpu"):
        rt.ensure_runtime()
    assert (rt.gpu_root() / "llama-server.exe").read_bytes() == b"MZ cuda"
    assert rt.server_path() is None


def test_abandoned_install_dirs_are_swept(monkeypatch, tmp_path, windows_x64):
    import os
    import time

    parent = rt.runtime_root().parent
    dead = parent / ".install-b0-dead"
    live = parent / ".install-b0-live"
    for d in (dead, live):
        (d / "tree").mkdir(parents=True)
        (d / "tree" / "f").write_bytes(b"x")
    old = time.time() - 2 * 3600
    for p in (dead / "tree" / "f", dead / "tree", dead):
        os.utime(p, (old, old))
    asset = rt.cpu_asset()
    good = _zip_bytes({"llama-server.exe": b"MZ"})
    monkeypatch.setitem(rt._ASSET_SHA256, asset, _sha(good))
    _serve(monkeypatch, {asset: good})
    rt.ensure_runtime()
    assert not dead.exists() and (live / "tree" / "f").exists()
    assert [p.name for p in parent.glob(".install-*")] == [live.name]


# ---- catalog GGUF -------------------------------------------------------------


@pytest.fixture
def models_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(cat, "MODELS_DIR", tmp_path)
    monkeypatch.setattr(cat, "ensure_dirs", lambda: None)  # never touch ~/.evi
    return tmp_path


def _entry(**over) -> dict:
    e = {"id": "t", "name": "T", "hf_repo": "acme/T-GGUF", "filename": "t.gguf",
         "revision": "1" * 40, "sha256": _sha(b"GGUF genuine weights")}
    e.update(over)
    return e


def test_tampered_gguf_rejected_and_removed(monkeypatch, models_dir):
    seen = _serve(monkeypatch, {"t.gguf": b"GGUF swapped weights"})
    entry = _entry()
    with pytest.raises(rt.IntegrityError):
        cat.download(entry)
    assert not cat.is_installed(entry)
    assert _files_under(models_dir) == []
    assert seen == [f"https://huggingface.co/acme/T-GGUF/resolve/{'1' * 40}/t.gguf"]


def test_genuine_gguf_downloads(monkeypatch, models_dir):
    _serve(monkeypatch, {"t.gguf": b"GGUF genuine weights"})
    entry = _entry()
    assert cat.download(entry).read_bytes() == b"GGUF genuine weights"
    assert cat.is_installed(entry)


def test_unpinned_gguf_is_never_fetched(monkeypatch, models_dir):
    _no_network(monkeypatch)
    entry = _entry()
    del entry["sha256"]
    with pytest.raises(rt.IntegrityError, match="No pinned sha256"):
        cat.download(entry)
    assert _files_under(models_dir) == []


def test_unpinned_file_at_the_catalog_path_is_not_trusted(monkeypatch, models_dir):
    """`evi models pull hf:<repo>:<file>` (unpinned, from main) writes the very
    path the catalog uses. The catalog must not load those bytes as if they
    were the pinned model: they are re-checked and replaced."""
    entry = _entry()
    path = cat.model_path(entry)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"GGUF pulled from a compromised main")   # what the hf: pull wrote
    assert not cat.is_installed(entry)

    seen = _serve(monkeypatch, {"t.gguf": b"GGUF genuine weights"})
    assert cat.download(entry).read_bytes() == b"GGUF genuine weights"
    assert seen == [f"https://huggingface.co/acme/T-GGUF/resolve/{'1' * 40}/t.gguf"]
    assert cat.is_installed(entry)


def test_installed_gguf_is_hashed_once_and_rechecked_when_replaced(monkeypatch, models_dir):
    import os

    entry = _entry()
    path = cat.model_path(entry)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"GGUF genuine weights")   # e.g. downloaded before pinning
    hashed: list[Path] = []
    real = cat._hash_file
    monkeypatch.setattr(cat, "_hash_file", lambda p: hashed.append(p) or real(p))
    _no_network(monkeypatch)

    assert cat.is_installed(entry) and cat.is_installed(entry)
    assert cat.download(entry) == path               # trusted: nothing fetched
    assert hashed == [path]                          # one hash, then the cached digest

    # Something else rewrites the file (same size, so only the stamp tells).
    path.write_bytes(b"GGUF swapped weights")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    assert not cat.is_installed(entry)
    assert len(hashed) == 2


def test_gguf_falls_back_to_main_when_the_pinned_commit_is_gone(monkeypatch, models_dir):
    """An upstream history squash 404s the pinned commit; the same bytes from
    main are accepted only because they match the same sha256."""
    import urllib.error

    seen: list[str] = []

    def fake_urlopen(req, timeout=None):
        seen.append(req.full_url)
        if "/resolve/main/" not in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 404, "Revision Not Found", {}, None)
        return _Resp(payload)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    entry = _entry()
    payload = b"GGUF genuine weights"
    assert cat.download(entry).read_bytes() == payload
    assert seen == [cat.download_url(entry), cat.download_url(entry, "main")]

    cat.remove(entry)
    payload = b"GGUF main moved on"                  # main no longer holds the pinned bytes
    with pytest.raises(rt.IntegrityError):
        cat.download(entry)
    assert _files_under(models_dir) == []


# ---- extraction -----------------------------------------------------------------


def _extract_tar(tmp_path: Path, members) -> Path:
    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(_tar_bytes(members))
    target = tmp_path / "box" / "target"
    rt._extract(archive, target)
    return target


@pytest.mark.parametrize("bad", [
    ("../x", b"escaped"),
    ("sub/../../x", b"escaped"),
    ("/abs/x", b"absolute"),
    ("C:/x", b"drive"),
    _link("lnk", "../../x"),                   # symlink out of the target
    _link("lnk", "/etc/passwd"),               # absolute symlink
    _link("hard", "../../x", tarfile.LNKTYPE),  # hardlink out of the target
], ids=["dotdot", "nested-dotdot", "absolute", "drive", "symlink-out", "symlink-abs",
        "hardlink-out"])
def test_tar_escape_writes_nothing(tmp_path, bad):
    # A benign member FIRST proves the check is all-or-nothing: the whole
    # archive is refused before a single byte is written.
    with pytest.raises(rt.IntegrityError):
        _extract_tar(tmp_path, [("ok/first.txt", b"benign"), bad])
    assert sorted(p.relative_to(tmp_path).as_posix() for p in _files_under(tmp_path)) == [
        "a.tar.gz"]
    assert not (tmp_path / "x").exists() and not (tmp_path / "box" / "x").exists()


def test_tar_device_member_refused(tmp_path):
    dev = tarfile.TarInfo("dev-null")
    dev.type = tarfile.CHRTYPE
    with pytest.raises(rt.IntegrityError):
        _extract_tar(tmp_path, [dev])
    assert not (tmp_path / "box" / "target" / "dev-null").exists()


def test_tar_with_internal_links_extracts(tmp_path):
    """The real llama.cpp tarballs ship relative .so/.dylib symlinks inside the
    archive dir; the filter must keep accepting them."""
    target = _extract_tar(tmp_path, [
        ("llama-b/libggml.so.0.20.0", b"lib"),
        _link("llama-b/libggml.so.0", "libggml.so.0.20.0"),
        ("llama-b/llama-server", b"bin"),
    ])
    assert (target / "llama-b" / "llama-server").read_bytes() == b"bin"
    assert (target / "llama-b" / "libggml.so.0.20.0").read_bytes() == b"lib"


@pytest.mark.parametrize("name", ["../x", "a/../../x", "/abs/x", "\\abs\\x", "C:/x",
                                  "C:x", "..\\x", "sub\\..\\..\\x"])
def test_zip_escape_writes_nothing(tmp_path, name):
    archive = tmp_path / "a.zip"
    archive.write_bytes(_zip_bytes({"ok/first.txt": b"benign", name: b"escaped"}))
    target = tmp_path / "box" / "target"
    with pytest.raises(rt.IntegrityError):
        rt._extract(archive, target)
    assert [p.name for p in _files_under(tmp_path)] == ["a.zip"]


def test_zip_benign_extracts(tmp_path):
    archive = tmp_path / "a.zip"
    archive.write_bytes(_zip_bytes({"llama-server.exe": b"MZ", "sub/ggml.dll": b"dll"}))
    target = tmp_path / "t"
    rt._extract(archive, target)
    assert (target / "llama-server.exe").read_bytes() == b"MZ"
    assert (target / "sub" / "ggml.dll").read_bytes() == b"dll"


# ---- notify: toast text is argv, never AppleScript -----------------------------------

_HOSTILE = [
    'He said "done" \\" & (do shell script "touch /tmp/pwned") & "',
    'end run\non run argv\ndo shell script "id"',
    "back\\slash \\\\ and 'single' quotes",
    "-e",
    "--help",
    "",
    "émoji ✓ 完成",
]


@pytest.mark.parametrize("title", ["eVi", *_HOSTILE])
@pytest.mark.parametrize("body", ["Turn complete", *_HOSTILE])
def test_osascript_toast_text_is_argv_only(title, body):
    argv = notify._osascript_toast_argv(title, body)
    sep = argv.index("--")
    script = argv[1:sep]
    # The AppleScript source is fixed: it never contains (part of) the text.
    assert script == list(notify._OSASCRIPT_TOAST)
    # The text is passed through verbatim as the two arguments after "--".
    assert argv[sep + 1:] == [title, body]
    # The handler reads the LAST two argv items, so the text it shows is right
    # whether osascript strips "--" or passes it through.
    handler = " ".join(script)
    assert "item -1 of argv" in handler.split("with title")[0]  # body
    assert "with title (item -2 of argv)" in handler
    for passed in (argv[sep + 1:], argv[sep:]):
        assert passed[-1] == body and passed[-2] == title


def test_desktop_toast_runs_fixed_script_on_macos(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(notify.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(notify.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(notify.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    title, body = 'eVi "x"', 'x" & (do shell script "touch /tmp/pwned") & "'
    notify._desktop_toast(title, body)
    assert calls == [notify._osascript_toast_argv(title, body)]
    assert not any(body in a or "pwned" in a for a in calls[0][:-2])


def test_desktop_toast_on_linux_ends_notify_send_options(monkeypatch):
    """A title/body starting with "-" is text for notify-send, not an option."""
    calls: list[list[str]] = []
    monkeypatch.setattr(notify.platform, "system", lambda: "Linux")
    monkeypatch.setattr(notify.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(notify.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    notify._desktop_toast("--help", "-u critical -t 0")
    assert calls == [["notify-send", "--", "--help", "-u critical -t 0"]]


# ---- pin script --------------------------------------------------------------------


def test_catalog_formatter_round_trips():
    """A re-pin rewrites the catalog without reformatting what it didn't change."""
    raw = (ROOT / "evi" / "data" / "runtime_models.json").read_text(encoding="utf-8")
    assert pin.format_catalog(json.loads(raw)) == raw, (
        "evi/data/runtime_models.json is not in the layout scripts/pin_runtime_hashes.py "
        "writes (2-space JSON, short lists inline); normalize it with "
        "`python scripts/pin_runtime_hashes.py --format`")


def test_pin_script_format_normalizes_a_hand_edit_offline(monkeypatch, tmp_path):
    catalog_json = tmp_path / "runtime_models.json"
    tidy = pin.format_catalog({"version": 1, "models": [
        {"id": "m", "min_ram_gb": 0.4, "tags": ["a", "b"], "ctx": {"native": 32768}}]})
    catalog_json.write_text(tidy.replace("0.4", "0.40").replace('["a", "b"]', '[ "a","b" ]'),
                            encoding="utf-8")
    monkeypatch.setattr(pin, "CATALOG_JSON", catalog_json)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("network"))
    assert pin.main(["--format"]) == 0
    assert catalog_json.read_text(encoding="utf-8") == tidy
    assert pin.main(["--format"]) == 0               # idempotent


def _fake_upstream(release: dict[str, str], hf: dict[tuple[str, str], tuple[str, str]]):
    """A fetch() for the script: GitHub release digests + HF revision metadata."""
    def fetch(url: str, headers: dict) -> dict:
        if "api.github.com" in url:
            return {"assets": [{"name": n, "digest": f"sha256:{d}"} for n, d in release.items()]
                    + [{"name": "no-digest.zip"}]}
        repo_rev = url.split("/api/models/", 1)[1].split("?", 1)[0]
        repo, rev = repo_rev.split("/revision/")
        commit, files = hf[(repo, rev)]
        return {"sha": commit, "siblings": [{"rfilename": f, "lfs": {"sha256": s}}
                                            for f, s in files.items()]}
    return fetch


def test_pin_script_writes_pins(monkeypatch, tmp_path):
    fake_rt = _fake_rt(
        LLAMACPP_VERSION="b1",
        _CPU_ASSETS={("linux", "x86_64"): "llama-b1-bin-ubuntu-x64.tar.gz"},
        _CUDA_ASSETS={"x86_64": {"12.4": ("llama-b1-cuda.zip", "cudart.zip")}},
    )
    runtime_py = tmp_path / "llamacpp_runtime.py"
    runtime_py.write_text(f"X = 1\n{pin.BEGIN}\n_ASSET_SHA256 = {{}}\n{pin.END}\nY = 2\n",
                          encoding="utf-8")
    catalog_json = tmp_path / "runtime_models.json"
    catalog_json.write_text(pin.format_catalog({"version": 1, "models": [
        {"id": "m", "hf_repo": "acme/M", "filename": "m.gguf", "tags": ["a", "b"]},
        {"id": "old", "hf_repo": "acme/O", "filename": "o.gguf",
         "revision": "2" * 40, "sha256": "f" * 64},
    ]}), encoding="utf-8")
    monkeypatch.setattr(pin, "RUNTIME_PY", runtime_py)
    monkeypatch.setattr(pin, "CATALOG_JSON", catalog_json)
    fetch = _fake_upstream(
        {"llama-b1-bin-ubuntu-x64.tar.gz": "a" * 64, "llama-b1-cuda.zip": "b" * 64,
         "cudart.zip": "c" * 64, "unrelated.zip": "d" * 64},
        {("acme/M", "main"): ("3" * 40, {"m.gguf": "e" * 64}),
         ("acme/O", "2" * 40): ("2" * 40, {"o.gguf": "f" * 64})},
    )

    assert pin.cmd_write(fake_rt, runtime=True, models=True, models_latest=False,
                         fetch=fetch) == 0

    src = runtime_py.read_text(encoding="utf-8")
    assert src.startswith("X = 1\n") and src.endswith("\nY = 2\n")
    block = src.split(pin.BEGIN)[1].split(pin.END)[0]
    assert ast.literal_eval(block.split("=", 1)[1].strip()) == {
        "llama-b1-bin-ubuntu-x64.tar.gz": "a" * 64,
        "llama-b1-cuda.zip": "b" * 64,
        "cudart.zip": "c" * 64,
    }
    models = json.loads(catalog_json.read_text(encoding="utf-8"))["models"]
    # A new model is pinned at main's current commit, keys right after filename.
    assert list(models[0])[:5] == ["id", "hf_repo", "filename", "revision", "sha256"]
    assert (models[0]["revision"], models[0]["sha256"]) == ("3" * 40, "e" * 64)
    # An already-pinned model is re-read at ITS revision, not moved to main.
    assert (models[1]["revision"], models[1]["sha256"]) == ("2" * 40, "f" * 64)
    assert '"tags": ["a", "b"]' in catalog_json.read_text(encoding="utf-8")


def _pin_files(monkeypatch, tmp_path, pins: dict[str, str], models: list[dict]):
    runtime_py = tmp_path / "llamacpp_runtime.py"
    runtime_py.write_text(f"X = 1\n{pin.render_block(pins)}\nY = 2\n", encoding="utf-8")
    catalog_json = tmp_path / "runtime_models.json"
    catalog_json.write_text(pin.format_catalog({"version": 1, "models": models}),
                            encoding="utf-8")
    monkeypatch.setattr(pin, "RUNTIME_PY", runtime_py)
    monkeypatch.setattr(pin, "CATALOG_JSON", catalog_json)
    return runtime_py, catalog_json


def test_pin_script_refuses_to_silently_repin_a_replaced_asset(monkeypatch, tmp_path, capsys):
    """Asset names carry the release tag, so a new digest under an already-pinned
    name means upstream replaced the asset: exactly what the pin must catch."""
    asset = "llama-b1-bin-ubuntu-x64.tar.gz"
    fake_rt = _fake_rt(LLAMACPP_VERSION="b1", _CPU_ASSETS={("linux", "x86_64"): asset},
                       _CUDA_ASSETS={}, _ASSET_SHA256={asset: "a" * 64})
    runtime_py, catalog_json = _pin_files(monkeypatch, tmp_path, {asset: "a" * 64}, [
        {"id": "new", "hf_repo": "acme/N", "filename": "n.gguf"}])
    before = (runtime_py.read_bytes(), catalog_json.read_bytes())
    fetch = _fake_upstream({asset: "b" * 64}, {("acme/N", "main"): ("3" * 40, {"n.gguf": "e" * 64})})

    assert pin.cmd_write(fake_rt, runtime=True, models=True, models_latest=False,
                         fetch=fetch) == 1
    err = capsys.readouterr().err
    assert f"CHANGED   runtime asset {asset}: pinned {'a' * 64}, upstream now {'b' * 64}" in err
    assert (runtime_py.read_bytes(), catalog_json.read_bytes()) == before   # nothing written

    # Only an explicit, deliberate flag takes the new digest.
    assert pin.cmd_write(fake_rt, runtime=True, models=True, models_latest=False,
                         repin_runtime=True, fetch=fetch) == 0
    assert f'"{asset}":\n        "{"b" * 64}"' in runtime_py.read_text(encoding="utf-8")


def test_pin_script_refuses_a_changed_file_at_a_pinned_commit(monkeypatch, tmp_path, capsys):
    """A commit's content can't legitimately change, so a different LFS sha256
    at the pinned commit is refused (not quietly taken)."""
    fake_rt = _fake_rt(LLAMACPP_VERSION="b1", _CPU_ASSETS={}, _CUDA_ASSETS={})
    model = {"id": "m", "hf_repo": "acme/M", "filename": "m.gguf",
             "revision": "2" * 40, "sha256": "0" * 64}
    _, catalog_json = _pin_files(monkeypatch, tmp_path, {}, [model])
    before = catalog_json.read_bytes()
    fetch = _fake_upstream({}, {("acme/M", "2" * 40): ("2" * 40, {"m.gguf": "f" * 64})})
    assert pin.cmd_write(fake_rt, runtime=False, models=True, models_latest=False,
                         fetch=fetch) == 1
    assert "CHANGED   model m: commit" in capsys.readouterr().err
    assert catalog_json.read_bytes() == before


def test_pin_script_refuses_a_release_without_digests(monkeypatch, tmp_path):
    fake_rt = _fake_rt(LLAMACPP_VERSION="b1", _CPU_ASSETS={("linux", "x86_64"): "no-digest.zip"},
                       _CUDA_ASSETS={})
    runtime_py = tmp_path / "rt.py"
    runtime_py.write_text(f"{pin.BEGIN}\n{pin.END}\n", encoding="utf-8")
    monkeypatch.setattr(pin, "RUNTIME_PY", runtime_py)
    with pytest.raises(pin.PinError, match="no-digest.zip"):
        pin.cmd_write(fake_rt, runtime=True, models=False, models_latest=False,
                      fetch=_fake_upstream({}, {}))
    assert runtime_py.read_text(encoding="utf-8") == f"{pin.BEGIN}\n{pin.END}\n"


def test_pin_script_check_passes_on_the_repo(capsys):
    assert pin.main(["--check"]) == 0
    assert "every runtime asset and catalog model is pinned" in capsys.readouterr().out
