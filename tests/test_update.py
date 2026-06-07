"""Tests for evi/update.py — PyPI probe, install-kind, snapshots, pip dispatch."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from evi.update import (
    Snapshot,
    UpdateError,
    _version_lt,
    apply_rollback,
    apply_upgrade,
    check_pypi,
    create_snapshot,
    detect_install_kind,
    gc_snapshots,
    list_snapshots,
    resolve_snapshot,
    verify_install,
)


# ----- _version_lt ----------------------------------------------------------


def test_version_lt_simple() -> None:
    assert _version_lt("0.10.0", "0.11.0")
    assert _version_lt("0.10.0", "0.10.1")
    assert _version_lt("0.10.0", "1.0.0")
    assert not _version_lt("0.11.0", "0.10.0")
    assert not _version_lt("0.11.0", "0.11.0")


def test_version_lt_prerelease() -> None:
    # Pre-release sorts BEFORE the same release version.
    assert _version_lt("0.11.0a1", "0.11.0")
    assert _version_lt("0.11.0b1", "0.11.0rc1")
    assert _version_lt("0.11.0rc1", "0.11.0")


def test_version_lt_unknown_shape_is_greater() -> None:
    # Anything that doesn't match the regex sorts as max — defensive default.
    assert not _version_lt("weird", "0.11.0")


# ----- check_pypi ----------------------------------------------------------


def _pypi_payload(version: str) -> dict:
    return {
        "info": {
            "version": version,
            "summary": "Local-first personal AI assistant",
            "project_url": "https://pypi.org/project/evi/",
        }
    }


def test_check_pypi_up_to_date() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_pypi_payload("0.11.0"))

    transport = httpx.MockTransport(handler)
    info = check_pypi(transport=transport, current="0.11.0")
    assert info.current == "0.11.0"
    assert info.latest == "0.11.0"
    assert info.behind is False


def test_check_pypi_behind() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_pypi_payload("0.12.0"))

    transport = httpx.MockTransport(handler)
    info = check_pypi(transport=transport, current="0.11.0")
    assert info.behind is True
    assert info.latest == "0.12.0"


def test_check_pypi_network_error_wraps() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    with pytest.raises(UpdateError, match="PyPI unreachable"):
        check_pypi(transport=transport, current="0.11.0")


def test_check_pypi_malformed_response() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"info": {}})  # no version

    transport = httpx.MockTransport(handler)
    with pytest.raises(UpdateError, match="missing info.version"):
        check_pypi(transport=transport, current="0.11.0")


# ----- detect_install_kind -----------------------------------------------


def _pip_show_proc(stdout: str, returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


def test_detect_install_kind_editable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **_):
        if cmd[-1] == "evi-assistant":
            return _pip_show_proc(
                "Name: evi-assistant\nVersion: 0.11.0\nEditable project location: /home/u/evi\n"
            )
        return _pip_show_proc("")

    monkeypatch.setattr("evi.update.subprocess.run", fake_run)
    kind = detect_install_kind()
    assert kind.kind == "editable"
    assert "/home/u/evi" in kind.location


def test_detect_install_kind_pipx_via_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PIPX_HOME", str(tmp_path))
    monkeypatch.setattr(
        "evi.update.sys",
        SimpleNamespace(executable=str(tmp_path / "venvs" / "evi" / "bin" / "python")),
    )
    # Patch subprocess to avoid the editable path firing first.
    monkeypatch.setattr(
        "evi.update.subprocess.run",
        lambda cmd, **_: _pip_show_proc(""),
    )
    kind = detect_install_kind()
    assert kind.kind == "pipx"


def test_detect_install_kind_pipx_via_path_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Even without PIPX_HOME, pipx's default layout (~/.local/pipx/venvs/evi-assistant/...)
    should be detected from sys.executable's path."""
    monkeypatch.delenv("PIPX_HOME", raising=False)
    monkeypatch.setattr(
        "evi.update.sys",
        SimpleNamespace(executable="/home/u/.local/pipx/venvs/evi-assistant/bin/python"),
    )
    monkeypatch.setattr(
        "evi.update.subprocess.run",
        lambda cmd, **_: _pip_show_proc(""),
    )
    kind = detect_install_kind()
    assert kind.kind == "pipx"


def test_detect_install_kind_locked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Set up a fake project with a poetry.lock above cwd, INSIDE the home tree.
    fake_home = tmp_path / "home"
    project = fake_home / "myproj"
    project.mkdir(parents=True)
    (project / "poetry.lock").write_text("# fake\n")
    monkeypatch.setattr("evi.update.Path.home", staticmethod(lambda: fake_home))
    monkeypatch.chdir(project)
    monkeypatch.delenv("PIPX_HOME", raising=False)
    monkeypatch.setattr(
        "evi.update.sys",
        SimpleNamespace(executable="/usr/bin/python"),
    )
    monkeypatch.setattr(
        "evi.update.subprocess.run",
        lambda cmd, **_: _pip_show_proc(""),  # not editable
    )
    kind = detect_install_kind()
    assert kind.kind == "locked"
    assert "poetry.lock" in kind.location


def test_detect_install_kind_venv_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No editable marker, no pipx, no lockfile → venv."""
    fake_home = tmp_path / "home"
    project = fake_home / "clean"
    project.mkdir(parents=True)
    monkeypatch.setattr("evi.update.Path.home", staticmethod(lambda: fake_home))
    monkeypatch.chdir(project)
    monkeypatch.delenv("PIPX_HOME", raising=False)
    monkeypatch.setattr(
        "evi.update.sys",
        SimpleNamespace(executable="/opt/venv/bin/python"),
    )
    monkeypatch.setattr(
        "evi.update.subprocess.run",
        lambda cmd, **_: _pip_show_proc(""),
    )
    kind = detect_install_kind()
    assert kind.kind == "venv"
    assert kind.upgradable is True


# ----- snapshots ---------------------------------------------------------


def test_create_snapshot_writes_files(tmp_path: Path) -> None:
    fake_freeze = "evi==0.11.0\nhttpx==0.27.0\n"
    fake_pip = ["echo-pip"]

    def fake_run(cmd, **kwargs):
        assert cmd[:1] == fake_pip
        assert cmd[-1] == "freeze"
        return SimpleNamespace(returncode=0, stdout=fake_freeze, stderr="")

    with patch("evi.update.subprocess.run", side_effect=fake_run):
        snap = create_snapshot(
            "0.11.0", "0.12.0", root=tmp_path, pip_executable=fake_pip,
        )
    assert snap.dir.is_dir()
    assert (snap.dir / "requirements.txt").read_text(encoding="utf-8") == fake_freeze
    assert (snap.dir / "version.txt").read_text(encoding="utf-8").strip() == "0.11.0"
    meta = json.loads((snap.dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta["from"] == "0.11.0"
    assert meta["to"] == "0.12.0"


def test_create_snapshot_cleans_up_on_pip_failure(tmp_path: Path) -> None:
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="oh no")

    with patch("evi.update.subprocess.run", side_effect=fake_run):
        with pytest.raises(UpdateError, match="pip freeze exited"):
            create_snapshot("0.11.0", "0.12.0", root=tmp_path, pip_executable=["x"])
    # Directory shouldn't be left lying around.
    assert list(tmp_path.iterdir()) == []


def _seed_snapshot(root: Path, ts: str, frm: str, to: str) -> Path:
    d = root / f"{ts}_{frm}_to_{to}"
    d.mkdir(parents=True)
    (d / "requirements.txt").write_text("evi==" + frm + "\n", encoding="utf-8")
    (d / "version.txt").write_text(frm + "\n", encoding="utf-8")
    (d / "metadata.json").write_text(json.dumps({"from": frm, "to": to}))
    return d


def test_list_snapshots_sorts_newest_first(tmp_path: Path) -> None:
    _seed_snapshot(tmp_path, "2026-01-01T10-00-00", "0.10.0", "0.11.0")
    _seed_snapshot(tmp_path, "2026-03-01T10-00-00", "0.11.0", "0.12.0")
    _seed_snapshot(tmp_path, "2026-02-01T10-00-00", "0.10.5", "0.11.0")
    snaps = list_snapshots(root=tmp_path)
    assert [s.timestamp.month for s in snaps] == [3, 2, 1]


def test_list_snapshots_skips_unrelated_dirs(tmp_path: Path) -> None:
    _seed_snapshot(tmp_path, "2026-01-01T10-00-00", "0.10.0", "0.11.0")
    (tmp_path / "random-folder").mkdir()
    (tmp_path / "almost_correct_but_no").mkdir()
    snaps = list_snapshots(root=tmp_path)
    assert len(snaps) == 1


def test_gc_snapshots_keeps_newest(tmp_path: Path) -> None:
    for i, ts in enumerate([
        "2026-01-01T10-00-00",
        "2026-02-01T10-00-00",
        "2026-03-01T10-00-00",
        "2026-04-01T10-00-00",
        "2026-05-01T10-00-00",
    ]):
        _seed_snapshot(tmp_path, ts, f"0.10.{i}", f"0.10.{i+1}")
    deleted = gc_snapshots(keep=2, root=tmp_path)
    assert len(deleted) == 3
    remaining = list_snapshots(root=tmp_path)
    assert len(remaining) == 2
    # The two newest survive.
    assert remaining[0].timestamp.month == 5
    assert remaining[1].timestamp.month == 4


def test_gc_snapshots_keep_zero_drops_all(tmp_path: Path) -> None:
    _seed_snapshot(tmp_path, "2026-01-01T10-00-00", "0.10.0", "0.11.0")
    assert gc_snapshots(keep=0, root=tmp_path)
    assert list_snapshots(root=tmp_path) == []


def test_resolve_snapshot_newest_by_default(tmp_path: Path) -> None:
    _seed_snapshot(tmp_path, "2026-01-01T10-00-00", "0.10.0", "0.11.0")
    _seed_snapshot(tmp_path, "2026-03-01T10-00-00", "0.11.0", "0.12.0")
    s = resolve_snapshot(None, root=tmp_path)
    assert s.timestamp.month == 3


def test_resolve_snapshot_by_index(tmp_path: Path) -> None:
    _seed_snapshot(tmp_path, "2026-01-01T10-00-00", "0.10.0", "0.11.0")
    _seed_snapshot(tmp_path, "2026-03-01T10-00-00", "0.11.0", "0.12.0")
    assert resolve_snapshot(2, root=tmp_path).timestamp.month == 1


def test_resolve_snapshot_by_name(tmp_path: Path) -> None:
    d = _seed_snapshot(tmp_path, "2026-01-01T10-00-00", "0.10.0", "0.11.0")
    s = resolve_snapshot(d.name, root=tmp_path)
    assert s.from_version == "0.10.0"


def test_resolve_snapshot_out_of_range(tmp_path: Path) -> None:
    _seed_snapshot(tmp_path, "2026-01-01T10-00-00", "0.10.0", "0.11.0")
    with pytest.raises(UpdateError, match="out of range"):
        resolve_snapshot(5, root=tmp_path)


def test_resolve_snapshot_when_none_exists(tmp_path: Path) -> None:
    with pytest.raises(UpdateError, match="no snapshots"):
        resolve_snapshot(None, root=tmp_path)


# ----- apply_upgrade / apply_rollback -----------------------------------


def test_apply_upgrade_success() -> None:
    fake_proc = SimpleNamespace(returncode=0, stdout="ok", stderr="")
    with patch("evi.update.subprocess.run", return_value=fake_proc) as run:
        r = apply_upgrade("evi==0.12.0", pip_executable=["x"])
    assert r.ok
    assert r.spec == "evi==0.12.0"
    # Verify the command shape includes --upgrade and the spec.
    args = run.call_args.args[0]
    assert "install" in args
    assert "--upgrade" in args
    assert "evi==0.12.0" in args


def test_apply_upgrade_failure_captured() -> None:
    fake_proc = SimpleNamespace(returncode=1, stdout="", stderr="conflict")
    with patch("evi.update.subprocess.run", return_value=fake_proc):
        r = apply_upgrade("evi==0.12.0", pip_executable=["x"])
    assert not r.ok
    assert r.returncode == 1
    assert "conflict" in r.stderr


def test_apply_upgrade_subprocess_error_wrapped() -> None:
    def boom(*a, **kw):
        raise OSError("no pip")

    with patch("evi.update.subprocess.run", side_effect=boom):
        r = apply_upgrade("evi", pip_executable=["x"])
    assert not r.ok
    assert r.returncode == -1
    assert "no pip" in r.stderr


def test_apply_rollback_runs_pip_install_dash_r(tmp_path: Path) -> None:
    snap_dir = _seed_snapshot(tmp_path, "2026-01-01T10-00-00", "0.10.0", "0.11.0")
    snap = Snapshot(
        dir=snap_dir,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        from_version="0.10.0",
        to_version="0.11.0",
    )
    fake_proc = SimpleNamespace(returncode=0, stdout="ok", stderr="")
    with patch("evi.update.subprocess.run", return_value=fake_proc) as run:
        r = apply_rollback(snap, pip_executable=["x"])
    assert r.ok
    args = run.call_args.args[0]
    assert "install" in args
    assert "-r" in args
    assert str(snap_dir / "requirements.txt") in args


def test_apply_rollback_missing_requirements(tmp_path: Path) -> None:
    snap_dir = tmp_path / "broken"
    snap_dir.mkdir()
    snap = Snapshot(
        dir=snap_dir,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        from_version="0.10.0",
        to_version="0.11.0",
    )
    r = apply_rollback(snap)
    assert not r.ok
    assert "requirements.txt" in r.stderr


# ----- verify_install ---------------------------------------------------


def test_verify_install_success() -> None:
    fake_proc = SimpleNamespace(returncode=0, stdout="0.12.0", stderr="")
    with patch("evi.update.subprocess.run", return_value=fake_proc):
        v = verify_install()
    assert v.ok
    assert v.version == "0.12.0"


def test_verify_install_failure() -> None:
    fake_proc = SimpleNamespace(
        returncode=1, stdout="", stderr="ModuleNotFoundError: No module named 'evi'"
    )
    with patch("evi.update.subprocess.run", return_value=fake_proc):
        v = verify_install()
    assert not v.ok
    assert "ModuleNotFoundError" in v.err


def test_verify_install_subprocess_error() -> None:
    with patch("evi.update.subprocess.run", side_effect=OSError("no python")):
        v = verify_install()
    assert not v.ok
    assert "no python" in v.err
