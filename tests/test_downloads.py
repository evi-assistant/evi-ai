"""Tests for the HuggingFace direct-download helper."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from evi.downloads import HFRef, download_gguf, parse_hf_ref


# ---- parse_hf_ref --------------------------------------------------------


def test_parse_hf_ref_repo_only() -> None:
    ref = parse_hf_ref("hf:bartowski/Qwen2.5-14B-Instruct-GGUF")
    assert ref == HFRef(repo="bartowski/Qwen2.5-14B-Instruct-GGUF", filename=None)


def test_parse_hf_ref_with_filename() -> None:
    ref = parse_hf_ref(
        "hf:bartowski/Qwen2.5-14B-Instruct-GGUF:Qwen2.5-14B-Instruct-Q4_K_M.gguf"
    )
    assert ref == HFRef(
        repo="bartowski/Qwen2.5-14B-Instruct-GGUF",
        filename="Qwen2.5-14B-Instruct-Q4_K_M.gguf",
    )


def test_parse_hf_ref_rejects_non_hf() -> None:
    assert parse_hf_ref("qwen2.5:14b") is None
    assert parse_hf_ref("") is None


# ---- download_gguf -------------------------------------------------------


def _install_fake_hf_hub(
    monkeypatch: pytest.MonkeyPatch,
    *,
    list_files_return: list[str],
    download_path: Path,
) -> dict:
    """Inject a fake `huggingface_hub` module so we don't hit the network.

    Returns a dict the test can read to assert how the fake was called.
    """
    calls: dict = {"list": 0, "download": 0, "last_filename": None}

    class _Api:
        def list_repo_files(self, repo_id: str) -> list[str]:
            calls["list"] += 1
            calls["last_repo"] = repo_id
            return list_files_return

    def _download(*, repo_id: str, filename: str, local_dir: str) -> str:
        calls["download"] += 1
        calls["last_filename"] = filename
        calls["last_local_dir"] = local_dir
        # Mimic huggingface_hub's behavior: place the file at local_dir/filename.
        path = Path(local_dir) / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"FAKEGGUF")
        return str(path)

    module = types.ModuleType("huggingface_hub")
    module.HfApi = _Api  # type: ignore[attr-defined]
    module.hf_hub_download = _download  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "huggingface_hub", module)
    return calls


def test_download_gguf_with_explicit_filename(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _install_fake_hf_hub(
        monkeypatch, list_files_return=[], download_path=tmp_path
    )
    ref = HFRef(repo="acme/M-GGUF", filename="M-Q4_K_M.gguf")
    path = download_gguf(ref, dest_root=tmp_path)
    assert path.is_file()
    assert calls["list"] == 0  # filename provided, no listing needed
    assert calls["download"] == 1
    assert calls["last_filename"] == "M-Q4_K_M.gguf"


def test_download_gguf_picks_q4_when_no_filename(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _install_fake_hf_hub(
        monkeypatch,
        list_files_return=[
            "README.md",
            "M-F16.gguf",
            "M-Q4_K_M.gguf",
            "M-Q8.gguf",
        ],
        download_path=tmp_path,
    )
    ref = HFRef(repo="acme/M-GGUF", filename=None)
    path = download_gguf(ref, dest_root=tmp_path)
    assert path.is_file()
    assert calls["last_filename"] == "M-Q4_K_M.gguf"


def test_download_gguf_falls_back_to_first_gguf(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _install_fake_hf_hub(
        monkeypatch,
        list_files_return=["README.md", "M-Q8.gguf", "M-Q6.gguf"],
        download_path=tmp_path,
    )
    ref = HFRef(repo="acme/M-GGUF", filename=None)
    download_gguf(ref, dest_root=tmp_path)
    assert calls["last_filename"] == "M-Q8.gguf"


def test_download_gguf_errors_when_no_gguf(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_fake_hf_hub(
        monkeypatch, list_files_return=["README.md"], download_path=tmp_path
    )
    ref = HFRef(repo="acme/M-GGUF", filename=None)
    with pytest.raises(RuntimeError, match="no GGUF"):
        download_gguf(ref, dest_root=tmp_path)


def test_download_gguf_missing_huggingface_hub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Make `huggingface_hub` import fail to simulate an absent optional dep.
    monkeypatch.setitem(sys.modules, "huggingface_hub", None)
    with pytest.raises(RuntimeError, match="huggingface_hub"):
        download_gguf(HFRef(repo="x/y", filename="z.gguf"))
