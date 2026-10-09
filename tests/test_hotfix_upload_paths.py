"""1.0.53 hotfix: session ids must never walk an upload path off UPLOADS_DIR.

/api/upload and /api/transcribe built `UPLOADS_DIR / session_id` from a raw form
field, so `../..`, `C:/x` or `/abs` (pathlib swaps the base for an absolute
part) wrote anywhere the user could. Every join of UPLOADS_DIR with a session id
now goes through `valid_session_id` + a resolved-path containment check.

UPLOADS_DIR is a tmp dir wrapped in a tripwire Path subclass: if a regression
ever let a bad id through, the write is refused (and the test fails) instead of
landing outside the tmp tree.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("fastapi")

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import evi.apps.web.server as server_mod  # noqa: E402
from evi.apps.web.ids import SESSION_ID_MAX_LEN, valid_session_id  # noqa: E402
from evi.config import Config  # noqa: E402
from evi.llm.agent import Done, Event, TextDelta  # noqa: E402


class _Tripwire(type(Path())):
    """A Path that refuses to create or open anything outside `jail`."""

    jail: Path | None = None

    def _check(self) -> None:
        jail = type(self).jail
        assert jail is not None and self.resolve().is_relative_to(jail), (
            f"write escaped the tmp tree: {self}"
        )

    def mkdir(self, *args, **kwargs):  # type: ignore[override]
        self._check()
        return super().mkdir(*args, **kwargs)

    def open(self, *args, **kwargs):  # type: ignore[override]
        self._check()
        return super().open(*args, **kwargs)


class _FakeAgent:
    def __init__(self) -> None:
        self.config = Config()
        self.tools: dict = {}
        self.auto_approve_categories: set[str] = set()
        self.permission_callback = None
        self.history: list[dict] = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "look at secret.png and pic.png"},
        ]

    def chat(self, message: str, images=None, **_) -> Iterator[Event]:
        yield TextDelta(text="ok")
        yield Done(reason="stop")

    def reset(self) -> None:
        pass


@pytest.fixture
def uploads(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """UPLOADS_DIR two levels below tmp_path, so `../..` still lands in tmp."""
    monkeypatch.setattr(_Tripwire, "jail", tmp_path.resolve())
    udir = _Tripwire(tmp_path) / "home" / "uploads"
    udir.mkdir(parents=True)
    monkeypatch.setattr(server_mod, "UPLOADS_DIR", udir)
    return udir


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, uploads: Path) -> TestClient:
    import evi.sdk.builder as builder_mod
    import evi.sessions as sessions_mod

    monkeypatch.delenv("EVI_ALLOW_CROSS_ORIGIN", raising=False)
    monkeypatch.setattr(builder_mod, "build_agent", lambda *_, **__: _FakeAgent())
    monkeypatch.setattr(sessions_mod, "find_session", lambda *_, **__: None)
    monkeypatch.setattr(server_mod, "make_client", lambda *_: None)
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path / "images")
    monkeypatch.setattr(server_mod.Config, "load", classmethod(lambda cls: Config()))
    return TestClient(server_mod.create_app())


def _tree(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in Path(root).rglob("*")}


# Path-like or malformed ids. Each must be refused before touching the disk.
BAD_IDS = [
    "..", "../..", "..\\..", "a/../../b", "../../pwned", "C:/x", "C:x", "C:\\x",
    "/abs", "\\abs", "\\\\server\\share", "//server/share", ".", "a.b", "a b",
    " a", "a\t", "x\x00y", "a\nb", "a:b", "CON", "nul", "com1",
    "a" * (SESSION_ID_MAX_LEN + 1),
]
# Every shape eVi actually mints (see evi/apps/web/ids.py).
GOOD_IDS = [
    "3f2b8c1e-9d4a-4b6f-8e2a-1c5d7f9a0b3e",           # web tab: crypto.randomUUID()
    "a1b2c3d4e5f6",                                    # agent: token_hex(6)
    "0123456789abcdef",                                # branch: token_hex(8)
    "vscode",                                          # VS Code sidebar
    "telegram-123456789",                              # Telegram channel
    "claude-code-3f2b8c1e-9d4a-4b6f-8e2a-1c5d7f9a0b3e",  # imported
    "codex-rollout-2025-05-07T17-24-21-5973b6c0-94b8-487b-a530-2aeb6098ae0e",
    "s1", "sess_1", "a" * SESSION_ID_MAX_LEN,
]


# ---- the validator ---------------------------------------------------------


@pytest.mark.parametrize("sid", BAD_IDS)
def test_valid_session_id_rejects_path_like(sid: str) -> None:
    assert valid_session_id(sid) is False


@pytest.mark.parametrize("sid", GOOD_IDS)
def test_valid_session_id_accepts_every_minted_shape(sid: str) -> None:
    assert valid_session_id(sid) is True


@pytest.mark.parametrize("sid", ["", None, 123, b"abc"])
def test_valid_session_id_rejects_empty_and_non_str(sid) -> None:
    assert valid_session_id(sid) is False


@pytest.mark.parametrize("sid", BAD_IDS)
def test_session_upload_dir_raises_400(uploads: Path, sid: str) -> None:
    with pytest.raises(HTTPException) as exc:
        server_mod._session_upload_dir(sid)
    assert exc.value.status_code == 400


def test_within_requires_strict_containment(tmp_path: Path) -> None:
    root = tmp_path / "root"
    assert server_mod._within(root / "a", root)
    assert server_mod._within(root / "a" / "b.png", root)
    assert not server_mod._within(root, root)              # the root itself
    assert not server_mod._within(root / ".." / "x", root)
    assert not server_mod._within(tmp_path / "rootx", root)  # prefix, not child


# ---- /api/upload + /api/transcribe ----------------------------------------


@pytest.mark.parametrize("route", ["/api/upload", "/api/transcribe"])
@pytest.mark.parametrize("sid", [*BAD_IDS, ""])
def test_form_routes_refuse_bad_session_id(
    client: TestClient, tmp_path: Path, route: str, sid: str
) -> None:
    before = _tree(tmp_path)
    r = client.post(route, data={"session_id": sid},
                    files={"file": ("pwn.txt", b"owned", "text/plain")})
    # An empty form field never reaches the handler: FastAPI rejects it as
    # missing (422). Everything else is the handler's own 400.
    assert r.status_code == (422 if sid == "" else 400), (sid, r.text)
    assert _tree(tmp_path) == before, f"{route} wrote something for {sid!r}"


@pytest.mark.parametrize("sid", GOOD_IDS)
def test_upload_accepts_legit_ids(client: TestClient, uploads: Path, sid: str) -> None:
    r = client.post("/api/upload", data={"session_id": sid},
                    files={"file": ("note.txt", b"hello", "text/plain")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["size"] == 5
    assert (uploads / sid / "note.txt").read_bytes() == b"hello"
    assert Path(body["path"]) == uploads / sid / "note.txt"


def test_upload_strips_directories_from_filename(client: TestClient, uploads: Path) -> None:
    r = client.post("/api/upload", data={"session_id": "s1"},
                    files={"file": ("../../evil.txt", b"x", "text/plain")})
    assert r.status_code == 200
    assert (uploads / "s1" / "evil.txt").is_file()
    assert _tree(uploads.parent.parent) >= {"home/uploads/s1/evil.txt"}
    assert not (uploads.parent / "evil.txt").exists()


def test_transcribe_accepts_legit_id(
    client: TestClient, uploads: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import evi.voice as voice_mod

    monkeypatch.setattr(voice_mod, "transcribe_wav", lambda p: f"heard {Path(p).name}")
    sid = "3f2b8c1e-9d4a-4b6f-8e2a-1c5d7f9a0b3e"
    r = client.post("/api/transcribe", data={"session_id": sid},
                    files={"file": ("clip.wav", b"RIFF", "audio/wav")})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "text": "heard clip.wav",
                        "path": str(uploads / sid / "clip.wav")}


@pytest.mark.parametrize("name", ["..", "."])
def test_transcribe_rejects_dot_filenames(client: TestClient, name: str) -> None:
    r = client.post("/api/transcribe", data={"session_id": "s1"},
                    files={"file": (name, b"x", "audio/wav")})
    assert r.status_code == 400


# ---- GET /uploads/<sid>/<name> -------------------------------------------


# Bad ids that fit in one URL segment (a `/` can't reach the route at all).
SEGMENT_BAD = ["%2E%2E", "..%5C..", "C:x", "C:%5Cx", "a%20b", "a.b", "x%00y", "CON",
               "a" * (SESSION_ID_MAX_LEN + 1)]


@pytest.mark.parametrize("seg", SEGMENT_BAD)
def test_serve_upload_refuses_bad_session_id(
    client: TestClient, uploads: Path, seg: str
) -> None:
    # A file the traversal would reach if the id were honoured.
    (uploads.parent / "secret.png").write_bytes(b"png")
    r = client.get(f"/uploads/{seg}/secret.png")
    assert r.status_code == 400, (seg, r.status_code)


# On Windows `dir / "D:x"` is drive-relative — it drops `dir` entirely. On POSIX
# it's just an odd (missing) filename inside the dir.
_DRIVE_RELATIVE_STATUS = 400 if sys.platform == "win32" else 404


def test_serve_upload_refuses_drive_relative_name(client: TestClient, uploads: Path) -> None:
    (uploads / "s1").mkdir()
    r = client.get("/uploads/s1/D:secret.png")
    assert r.status_code == _DRIVE_RELATIVE_STATUS


def test_serve_upload_serves_legit(client: TestClient, uploads: Path) -> None:
    sid = "3f2b8c1e-9d4a-4b6f-8e2a-1c5d7f9a0b3e"
    (uploads / sid).mkdir()
    (uploads / sid / "pic.png").write_bytes(b"\x89PNG")
    r = client.get(f"/uploads/{sid}/pic.png")
    assert r.status_code == 200
    assert r.content == b"\x89PNG"


# ---- session history re-attaches only this session's own uploads ----------


def test_history_ignores_traversal_id(client: TestClient, uploads: Path) -> None:
    # `..\outside` would join to <home>/outside on Windows; its secret.png is
    # named in the user message, so a leak would show up as an image URL.
    outside = uploads.parent / "outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(b"png")
    r = client.get("/api/session/..%5Coutside/history")
    assert r.status_code == 400            # get_session refuses the id outright
    assert "secret.png" not in r.text


def test_history_reattaches_legit_uploads(client: TestClient, uploads: Path) -> None:
    sid = "a1b2c3d4e5f6"
    (uploads / sid).mkdir()
    (uploads / sid / "pic.png").write_bytes(b"png")
    r = client.get(f"/api/session/{sid}/history")
    assert r.status_code == 200
    user = [m for m in r.json()["messages"] if m["role"] == "user"]
    assert user[0]["images"] == [f"/uploads/{sid}/pic.png"]


# ---- /images/<name> --------------------------------------------------------


def test_images_refuses_drive_relative_name(client: TestClient) -> None:
    r = client.get("/images/D:secret.png")
    assert r.status_code == _DRIVE_RELATIVE_STATUS


# ---- one contract: every session-creating route refuses the same ids -------
#
# get_session hands the id to the agent's TranscriptStore, which writes
# `<day dir>/<id>.jsonl` — so `../../x` or `C:/x` on /api/chat wrote a transcript
# anywhere on disk. It now shares the upload routes' rule.


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> list[str]:
    """Session ids that reached build_agent (i.e. a TranscriptStore)."""
    import evi.sdk.builder as builder_mod

    seen: list[str] = []

    def fake_build(*_, session_id=None, **__):
        seen.append(session_id)
        return _FakeAgent()

    monkeypatch.setattr(builder_mod, "build_agent", fake_build)
    return seen


@pytest.mark.parametrize("sid", BAD_IDS)
def test_chat_refuses_bad_session_id(
    client: TestClient, built: list[str], tmp_path: Path, sid: str
) -> None:
    before = _tree(tmp_path)
    r = client.post("/api/chat", json={"session_id": sid, "message": "hi"})
    assert r.status_code == 400, (sid, r.text)
    assert "letters, digits" in r.json()["detail"]
    assert built == []                      # never reached an agent / transcript store
    assert _tree(tmp_path) == before


def test_chat_accepts_legit_ids(client: TestClient, built: list[str]) -> None:
    for sid in GOOD_IDS:
        r = client.post("/api/chat", json={"session_id": sid, "message": "hi"})
        assert r.status_code == 200, (sid, r.text)
    assert built == GOOD_IDS


# (method, path template, json body) for every route that creates/revives a
# session from a caller-supplied id.
SESSION_ROUTES = [
    ("POST", "/api/session/{}/name", {"name": "n"}),
    ("POST", "/api/session/{}/channel", {"text": "t"}),
    ("POST", "/api/session/{}/mode", {"mode": "chat"}),
    ("POST", "/api/session/{}/steer", {"text": "t"}),
    ("POST", "/api/session/{}/handoff", None),
    ("GET", "/api/session/{}/history", None),
]


@pytest.mark.parametrize("method,path,body", SESSION_ROUTES)
@pytest.mark.parametrize("seg", SEGMENT_BAD)
def test_session_routes_refuse_bad_session_id(
    client: TestClient, built: list[str], method: str, path: str, body, seg: str
) -> None:
    r = client.request(method, path.format(seg), json=body)
    assert r.status_code == 400, (method, path, seg, r.text)
    assert built == []


@pytest.mark.parametrize("sid", ["../../x", "C:\\x", "a.b"])
def test_session_cwd_refuses_bad_session_id(
    client: TestClient, built: list[str], tmp_path: Path, sid: str
) -> None:
    assert client.get("/api/session/cwd", params={"session_id": sid}).status_code == 400
    r = client.post("/api/session/cwd", json={"session_id": sid, "path": str(tmp_path)})
    assert r.status_code == 400
    assert built == []


def test_dotted_id_is_refused_consistently(client: TestClient, built: list[str]) -> None:
    """No half-supported ids: one that can't own an uploads dir can't chat either."""
    sid = "proj.notes"
    up = client.post("/api/upload", data={"session_id": sid},
                     files={"file": ("n.txt", b"x", "text/plain")})
    chat = client.post("/api/chat", json={"session_id": sid, "message": "hi"})
    assert up.status_code == chat.status_code == 400
    assert up.json()["detail"] == chat.json()["detail"]


# ---- `?session=<id>` links (handoff URL / evi://session deep link) -----------
#
# The web UI adopts that id as a tab and splices it into its own same-origin
# requests, so a crafted link from another site could steer them. The page is
# never served for a malformed id.


@pytest.mark.parametrize("query", [
    "session=..%2F..%2Fx", "session=..%5C..%5Cx", "session=C%3A%2Fx",
    "session=..%2Fruntime%2Fstop%3F", "session=a.b", "session=a%20b",
    "session=s1&session=..%2Fx",
])
@pytest.mark.parametrize("page", ["/", "/static/index.html"])
def test_page_refuses_malformed_session_link(
    client: TestClient, page: str, query: str
) -> None:
    r = client.get(f"{page}?{query}")
    assert r.status_code == 400, (page, query)
    assert "letters, digits" in r.json()["detail"]


@pytest.mark.parametrize("query", ["", "?session=", "?session=3f2b8c1e-9d4a-4b6f-8e2a-1c5d7f9a0b3e",
                                   "?session=codex-rollout-2025-05-07T17-24-21-5973b6c0",
                                   "?workflow=nightly"])
def test_page_serves_legit_session_link(client: TestClient, query: str) -> None:
    r = client.get(f"/{query}")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
