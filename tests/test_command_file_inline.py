"""@file inlining in slash-command templates comes only from the template.

`commands._substitute` used to splice the arguments into the template and then
inline every `@<path>` in the result, so `/cmd @~/.ssh/id_ed25519` (or an MCP
client's prompts/get with `{"args": "@~/.evi/config.toml"}`) read the file back.
Now only refs the template itself contains are inlined, confined to the working
folder or trusted dirs and to non-protected paths; arguments are plain text; and
MCP-served prompts never inline. Also: an unbalanced quote in the arguments
falls back to a whitespace split instead of raising (which 500ed /api/chat),
and one unreadable command file no longer breaks every other command.

`~` is pointed at a tmp home (HOME + USERPROFILE) holding fake secrets, so a
regression reads the fake key, never a real one.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Iterator

import pytest

import evi.commands as commands_mod
from evi import workdir
from evi.commands import CommandStore
from evi.config import Config
from evi.mcp import publish

SSH_KEY = "FAKE-SSH-PRIVATE-KEY"
EVI_SECRET = "FAKE-EVI-API-KEY"
NOTES = "NOTES-IN-WORKDIR"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp `~` with a fake ssh key and eVi config; also the working folder."""
    h = tmp_path / "home"
    (h / ".ssh").mkdir(parents=True)
    (h / ".ssh" / "id_ed25519").write_text(SSH_KEY, encoding="utf-8")
    (h / ".evi").mkdir()
    (h / ".evi" / "config.toml").write_text(f'api_key = "{EVI_SECRET}"\n', encoding="utf-8")
    (h / "notes.md").write_text(NOTES, encoding="utf-8")
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))  # Windows expanduser reads this
    assert Path("~/.ssh/id_ed25519").expanduser().read_text(encoding="utf-8") == SSH_KEY
    return h


@pytest.fixture
def cmds(tmp_path: Path) -> Path:
    d = tmp_path / "commands"
    d.mkdir()
    (d / "explain.md").write_text("Explain this: $ARGUMENTS\n", encoding="utf-8")
    (d / "legacy.md").write_text("Legacy: {args}\n", encoding="utf-8")
    (d / "first.md").write_text("First: $1\n", encoding="utf-8")
    (d / "review.md").write_text("Review @$1 please\n", encoding="utf-8")
    (d / "q.md").write_text("first=$1 second=$2 all=$ARGUMENTS\n", encoding="utf-8")
    (d / "ctx.md").write_text("Context: @notes.md\nTask: $ARGUMENTS\n", encoding="utf-8")
    return d


def _no_secret(text: str | None) -> None:
    assert text is not None
    assert SSH_KEY not in text and EVI_SECRET not in text


# ---- arguments never inline ------------------------------------------------


@pytest.mark.parametrize("name", ["explain", "legacy", "first"])
@pytest.mark.parametrize("ref", ["@~/.ssh/id_ed25519", "@~/.evi/config.toml"])
def test_argument_ref_is_text_not_file(home: Path, cmds: Path, name: str, ref: str) -> None:
    store = CommandStore(root=cmds)
    # Worst case: the working folder is ~ itself, so the files are "in scope".
    out = store.expand(name, ref, workdir=home)
    _no_secret(out)
    assert ref in out  # substituted verbatim


def test_absolute_argument_ref_is_text(home: Path, cmds: Path) -> None:
    ref = "@" + str(home / ".evi" / "config.toml")
    out = CommandStore(root=cmds).expand("explain", ref, workdir=home)
    _no_secret(out)
    assert ref in out


def test_template_ref_built_from_args_is_text(home: Path, cmds: Path) -> None:
    """`@$1` would let the arguments pick the path, so it is never read."""
    store = CommandStore(root=cmds)
    out = store.expand("review", "~/.ssh/id_ed25519", workdir=home)
    _no_secret(out)
    assert out == "Review @~/.ssh/id_ed25519 please"
    # ...even for a harmless file inside the working folder.
    assert store.expand("review", "notes.md", workdir=home) == "Review @notes.md please"


def test_argument_text_is_not_rescanned(home: Path, cmds: Path) -> None:
    """Args are literal: placeholders inside them aren't substituted again."""
    out = CommandStore(root=cmds).expand("explain", "cost is $1 {args}", workdir=home)
    assert out == "Explain this: cost is $1 {args}"


# ---- template-authored refs ------------------------------------------------


def test_template_ref_inside_workdir_inlined(home: Path, cmds: Path) -> None:
    out = CommandStore(root=cmds).expand("ctx", "summarise", workdir=home)
    assert NOTES in out and "```" in out
    assert out.endswith("Task: summarise")


def test_template_absolute_ref_inside_workdir_inlined(tmp_path: Path, cmds: Path) -> None:
    work = tmp_path / "work"
    (work / "docs").mkdir(parents=True)
    (work / "docs" / "style.md").write_text("STYLE GUIDE", encoding="utf-8")
    (cmds / "style.md").write_text(f"Follow @{work / 'docs' / 'style.md'}\n", encoding="utf-8")
    assert "STYLE GUIDE" in CommandStore(root=cmds).expand("style", "", workdir=work)


def test_default_workdir_is_session_working_folder(home: Path, cmds: Path) -> None:
    token = workdir.set_cwd(home)
    try:
        assert NOTES in CommandStore(root=cmds).expand("ctx", "x")
    finally:
        workdir.reset(token)


def test_template_ref_outside_workdir_needs_trusted_dir(tmp_path: Path, cmds: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "ref.md").write_text("FAR AWAY", encoding="utf-8")
    ref = elsewhere / "ref.md"
    (cmds / "far.md").write_text(f"See @{ref}\n", encoding="utf-8")
    store = CommandStore(root=cmds)

    out = store.expand("far", "", workdir=work)
    assert "FAR AWAY" not in out and f"@{ref}" in out  # left as-is
    out = store.expand("far", "", workdir=work, trusted_dirs=[str(elsewhere)])
    assert "FAR AWAY" in out


def test_template_traversal_out_of_workdir_not_inlined(tmp_path: Path, cmds: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / "outside.md").write_text("OUTSIDE", encoding="utf-8")
    (cmds / "up.md").write_text("See @../outside.md\n", encoding="utf-8")
    out = CommandStore(root=cmds).expand("up", "", workdir=work)
    assert "OUTSIDE" not in out and "@../outside.md" in out


@pytest.mark.parametrize("ref", ["@~/.ssh/id_ed25519", "@.ssh/id_ed25519", "@.env"])
def test_template_ref_to_protected_path_not_inlined(home: Path, cmds: Path, ref: str) -> None:
    (home / ".env").write_text(f"TOKEN={SSH_KEY}", encoding="utf-8")
    (cmds / "leak.md").write_text(f"Read {ref}\n", encoding="utf-8")
    out = CommandStore(root=cmds).expand("leak", "", workdir=home)
    assert SSH_KEY not in out and ref in out


def test_custom_protected_paths_respected(home: Path, cmds: Path) -> None:
    store = CommandStore(root=cmds)
    out = store.expand("ctx", "", workdir=home, protected_paths=["notes.*"])
    assert NOTES not in out and "@notes.md" in out


@pytest.mark.parametrize(
    "ref", ["@secrets/key", "@./secrets/key", "@sub/../secrets/key", "ABS", "TRUSTED"]
)
def test_protected_pattern_with_a_directory_holds_for_every_spelling(
    tmp_path: Path, home: Path, cmds: Path, ref: str
) -> None:
    """`secrets/*` used to match only the literal ref: `./`, `../` and absolute
    spellings of the same file slipped past it."""
    (home / "secrets").mkdir()
    (home / "secrets" / "key").write_text("SEGSECRET", encoding="utf-8")
    (home / "sub").mkdir()
    workdir = home
    trusted: list[str] = []
    if ref == "ABS":
        ref = f"@{home / 'secrets' / 'key'}"
    elif ref == "TRUSTED":  # relative to a trusted dir, not the working folder
        ref = f"@{home / 'secrets' / 'key'}"
        workdir = tmp_path / "work"
        workdir.mkdir()
        trusted = [str(home)]
    (cmds / "seg.md").write_text(f"Read {ref}\n", encoding="utf-8")
    store = CommandStore(root=cmds)
    out = store.expand(
        "seg", "", workdir=workdir, trusted_dirs=trusted, protected_paths=["secrets/*"]
    )
    assert "SEGSECRET" not in out and ref in out
    # The same ref inlines once nothing protects it (so the block above is real).
    assert "SEGSECRET" in store.expand(
        "seg", "", workdir=workdir, trusted_dirs=trusted, protected_paths=[]
    )


def test_home_relative_protected_pattern(home: Path, cmds: Path) -> None:
    """A `~/...` pattern (as in `~/.evi/**`) protects the file it names."""
    (home / ".evi" / "hooks.toml").write_text("HOOKS-FILE", encoding="utf-8")
    (cmds / "hooks.md").write_text("Read @.evi/hooks.toml\n", encoding="utf-8")
    store = CommandStore(root=cmds)
    out = store.expand("hooks", "", workdir=home, protected_paths=["~/.evi/**"])
    assert "HOOKS-FILE" not in out and "@.evi/hooks.toml" in out
    assert "HOOKS-FILE" in store.expand("hooks", "", workdir=home, protected_paths=[])


def test_protected_match_ignores_case(home: Path, cmds: Path) -> None:
    """On a case-insensitive disk (macOS) `@NOTES.md` opens notes.md."""
    (home / "NOTES.md").write_text(NOTES, encoding="utf-8")  # same file on Windows/macOS
    (cmds / "upper.md").write_text("Read @NOTES.md\n", encoding="utf-8")
    out = CommandStore(root=cmds).expand(
        "upper", "", workdir=home, protected_paths=["notes.md"]
    )
    assert NOTES not in out and "@NOTES.md" in out


@pytest.mark.parametrize(
    "rel",
    [
        ".ssh/id_ecdsa", ".ssh/config", ".netrc", ".aws/credentials", "deploy.key", ".env",
        # SSH keys anywhere (any type, _sk and .pub), tool login / token stores
        "keys/id_ed25519_sk", "backup/id_rsa.pub", ".git-credentials",
        ".config/git/credentials", ".npmrc", ".pypirc", ".pgpass", ".my.cnf",
        ".vault-token", ".cargo/credentials.toml", ".docker/config.json", ".kube/config",
        ".config/gh/hosts.yml", ".gnupg/private-keys-v1.d/key.key",
        # cloud CLIs and Terraform
        ".aws/sso/cache/token.json", ".config/gcloud/application_default_credentials.json",
        ".config/gcloud/credentials.db", "ci/application_default_credentials.json",
        "ci/my-service-account-key.json", ".azure/msal_token_cache.json",
        "infra/prod.tfvars", "infra/terraform.tfstate", "infra/terraform.tfstate.backup",
    ],
)
def test_secret_files_never_inline_even_when_unprotected(
    home: Path, cmds: Path, rel: str
) -> None:
    """Key and credential files stay out of prompts even with protected_paths = []."""
    f = home / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(SSH_KEY, encoding="utf-8")
    (cmds / "sec.md").write_text(f"Read @{rel} and @~/{rel}\n", encoding="utf-8")
    out = CommandStore(root=cmds).expand("sec", "", workdir=home, protected_paths=[])
    _no_secret(out)
    assert f"@{rel}" in out and f"@~/{rel}" in out


@pytest.mark.parametrize(
    "rel", ["notes/credentials.md", "config.json", "id_map.json", "docs/kube-config.md"]
)
def test_secret_list_spares_lookalike_files(home: Path, cmds: Path, rel: str) -> None:
    """The secret list names credential files, not everything that sounds like one."""
    f = home / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(NOTES, encoding="utf-8")
    (cmds / "ok.md").write_text(f"Read @{rel}\n", encoding="utf-8")
    assert NOTES in CommandStore(root=cmds).expand("ok", "", workdir=home, protected_paths=[])


def test_symlink_out_of_workdir_not_inlined(home: Path, tmp_path: Path, cmds: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    try:
        os.symlink(home / ".evi" / "config.toml", work / "innocent.md")
        ref = "@innocent.md"
    except (OSError, NotImplementedError):
        if sys.platform != "win32":
            pytest.skip("symlinks unavailable here")
        # Unprivileged Windows can't symlink, but a junction needs no rights.
        import _winapi

        _winapi.CreateJunction(str(home / ".evi"), str(work / "innocent"))
        ref = "@innocent/config.toml"
    assert EVI_SECRET in (work / ref[1:]).read_text(encoding="utf-8")  # the link works
    (cmds / "sym.md").write_text(f"See {ref}\n", encoding="utf-8")
    out = CommandStore(root=cmds).expand("sym", "", workdir=work)
    _no_secret(out)
    assert ref in out


@pytest.mark.skipif(sys.platform != "win32", reason="UNC and device paths are Windows-only")
@pytest.mark.parametrize(
    "ref",
    [
        r"@\\evil-host\share\x.md",
        "@//evil-host/share/x.md",
        r"@\\?\UNC\evil-host\share\x.md",
        r"@\\.\pipe\evil-host",
    ],
)
def test_network_template_ref_is_refused_before_any_io(
    tmp_path: Path, cmds: Path, monkeypatch: pytest.MonkeyPatch, ref: str
) -> None:
    """Resolving a UNC ref would open an SMB connection (and send NTLM) before
    the containment check could refuse it."""
    work = tmp_path / "work"
    work.mkdir()
    touched: list[str] = []
    real_realpath = os.path.realpath

    def spy(path, *a, **k):
        if "evil-host" in os.fspath(path).lower():
            touched.append(os.fspath(path))
            raise OSError("network access attempted")
        return real_realpath(path, *a, **k)

    monkeypatch.setattr(os.path, "realpath", spy)
    (cmds / "unc.md").write_text(f"See {ref}\n", encoding="utf-8")
    out = CommandStore(root=cmds).expand("unc", "", workdir=work)
    assert out == f"See {ref}"
    assert touched == []


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_fifo_template_ref_is_not_opened(tmp_path: Path, cmds: Path) -> None:
    """Opening a FIFO blocks until a writer appears; only regular files are read."""
    work = tmp_path / "work"
    work.mkdir()
    os.mkfifo(work / "pipe")
    (cmds / "fifo.md").write_text("See @pipe\n", encoding="utf-8")
    assert CommandStore(root=cmds).expand("fifo", "", workdir=work) == "See @pipe"


def test_large_template_ref_is_read_only_up_to_the_cap(tmp_path: Path, cmds: Path) -> None:
    """Only the capped prefix is read: bytes far past it aren't even decoded."""
    work = tmp_path / "work"
    work.mkdir()
    cap = commands_mod._MAX_INLINE_BYTES
    (work / "big.txt").write_bytes(b"a" * (cap + 10) + b"z" * 200_000 + b"\xff")
    (cmds / "big.md").write_text("Big: @big.txt\n", encoding="utf-8")
    out = CommandStore(root=cmds).expand("big", "", workdir=work)
    assert "a" * cap in out and "a" * (cap + 1) not in out and "z" not in out


def test_inlined_text_is_verbatim(tmp_path: Path, cmds: Path) -> None:
    """File contents aren't scanned for placeholders or nested @refs."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "other.md").write_text("NESTED", encoding="utf-8")
    (work / "tpl.sh").write_text('echo "$1" $ARGUMENTS {args} @other.md', encoding="utf-8")
    (cmds / "sh.md").write_text("Script: @tpl.sh\nArgs: $ARGUMENTS\n", encoding="utf-8")
    out = CommandStore(root=cmds).expand("sh", "zzz", workdir=work)
    assert 'echo "$1" $ARGUMENTS {args} @other.md' in out
    assert "NESTED" not in out
    assert out.endswith("Args: zzz")


def test_unreadable_template_ref_left_as_token(tmp_path: Path, cmds: Path) -> None:
    """A non-UTF-8 file or a directory is left as text, not an exception."""
    work = tmp_path / "work"
    (work / "adir").mkdir(parents=True)
    (work / "blob.bin").write_bytes(b"\xff\xfe\x00\x81binary")
    (cmds / "bin.md").write_text("A @blob.bin B @adir C @~nosuchuser/x\n", encoding="utf-8")
    out = CommandStore(root=cmds).expand("bin", "", workdir=work)
    assert out == "A @blob.bin B @adir C @~nosuchuser/x"


def test_inline_false_leaves_template_refs(home: Path, cmds: Path) -> None:
    out = CommandStore(root=cmds).expand("ctx", "t", inline=False, workdir=home)
    assert NOTES not in out and "Context: @notes.md" in out


def test_refused_template_ref_says_why(
    tmp_path: Path, home: Path, cmds: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A file the template names but may not read logs a warning (so a style
    guide that stops inlining isn't a mystery); plain `@words` stay quiet."""
    work = tmp_path / "work"
    work.mkdir()
    (work / ".env").write_text("TOKEN=x", encoding="utf-8")
    (tmp_path / "outside.md").write_text("OUTSIDE", encoding="utf-8")
    (cmds / "mix.md").write_text(
        "A @../outside.md B @.env C @alice D @/api/v1/users E @nosuch.md\n", encoding="utf-8"
    )
    caplog.set_level(logging.WARNING, logger="evi.commands")
    out = CommandStore(root=cmds).expand("mix", "", workdir=work)
    assert out == "A @../outside.md B @.env C @alice D @/api/v1/users E @nosuch.md"
    msgs = [r.getMessage() for r in caplog.records if r.name == "evi.commands"]
    assert len(msgs) == 2, msgs
    assert "@../outside.md" in msgs[0] and "outside the working folder" in msgs[0]
    assert "@.env" in msgs[1] and "protected" in msgs[1]


# ---- unbalanced quotes ------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "first", "second"),
    [
        ('"two words tail', '"two', "words"),   # unbalanced double quote
        ("don't panic", "don't", "panic"),      # an apostrophe is the common case
        ("trailing \\", "trailing", "\\"),      # shlex: "No escaped character"
        ('"two words" tail', "two words", "tail"),  # balanced → still shell-split
    ],
)
def test_bad_quoting_falls_back_to_whitespace_split(
    cmds: Path, args: str, first: str, second: str
) -> None:
    out = CommandStore(root=cmds).expand("q", args)
    assert out == f"first={first} second={second} all={args}"


# ---- MCP prompts/get never inlines -----------------------------------------


@pytest.fixture
def mcp_cmds(cmds: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Serve `cmds` as the MCP prompt store, with ~ (and the cwd) = tmp home."""
    monkeypatch.setattr(commands_mod, "COMMANDS_DIR", cmds)
    monkeypatch.chdir(home)
    token = workdir.set_cwd(home)
    yield cmds
    workdir.reset(token)


@pytest.mark.parametrize("ref", ["@~/.evi/config.toml", "@~/.ssh/id_ed25519"])
def test_mcp_expand_never_inlines_argument_refs(mcp_cmds: Path, ref: str) -> None:
    for name in ("explain", "legacy", "first", "review"):
        out = publish.expand_command_prompt(name, {"args": ref})
        _no_secret(out)


def test_mcp_expand_never_inlines_template_refs(mcp_cmds: Path) -> None:
    out = publish.expand_command_prompt("ctx", {"args": "go"})
    assert NOTES not in out and "Context: @notes.md" in out


async def test_mcp_prompts_get_round_trip_does_not_inline(mcp_cmds: Path) -> None:
    """A real MCP client calls prompts/get on eVi's server with an @path arg."""
    pytest.importorskip("mcp")
    import mcp

    server = publish.build_server(("git",))

    async def check(client) -> None:
        names = {p.name for p in (await client.list_prompts()).prompts}
        assert {"explain", "ctx"} <= names
        res = await client.get_prompt("explain", {"args": "@~/.evi/config.toml"})
        text = res.messages[0].content.text
        _no_secret(text)
        assert "@~/.evi/config.toml" in text
        res = await client.get_prompt("ctx", {"args": "go"})
        assert NOTES not in res.messages[0].content.text

    try:  # mcp 1.x — one helper yields a connected client session
        from mcp.shared.memory import (
            create_connected_server_and_client_session as _connect,
        )
    except ImportError:
        _connect = None

    if _connect is not None:
        async with _connect(server) as client:
            await check(client)
        return

    import anyio
    from mcp.shared.memory import create_client_server_memory_streams

    init_opts = server.create_initialization_options()
    async with create_client_server_memory_streams() as ((c_read, c_write), (s_read, s_write)):
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                lambda: server.run(s_read, s_write, init_opts, raise_exceptions=True)
            )
            async with mcp.ClientSession(c_read, c_write) as client:
                await client.initialize()
                await check(client)
            tg.cancel_scope.cancel()


# ---- web /api/chat slash path ----------------------------------------------


def _server():
    pytest.importorskip("fastapi")
    pytest.importorskip("sse_starlette")
    import evi.apps.web.server as server_mod

    return server_mod


class _SlashAgent:
    """Just the surface `_handle_slash` and the /api/chat worker touch."""

    def __init__(self, cwd: str = "") -> None:
        self.config = Config()
        self.cwd = cwd
        self.tools: dict = {}
        self.auto_approve_categories: set[str] = set()
        self.permission_callback = None
        self.messages: list[str] = []

    def chat(self, message: str, images=None, **_kwargs) -> Iterator:
        from evi.llm.agent import Done, TextDelta

        self.messages.append(message)
        yield TextDelta(text="ok")
        yield Done(reason="stop")

    def reset(self) -> None:
        pass


def test_handle_slash_argument_ref_not_inlined(home: Path, cmds: Path) -> None:
    server_mod = _server()
    agent = _SlashAgent(cwd=str(home))
    out = server_mod._handle_slash(agent, "/explain @~/.ssh/id_ed25519", CommandStore(root=cmds))
    assert out.handled is False
    _no_secret(out.expand_to)
    assert out.expand_to == "Explain this: @~/.ssh/id_ed25519"


def test_handle_slash_inlines_from_session_folder_and_trusted_dirs(
    tmp_path: Path, home: Path, cmds: Path
) -> None:
    server_mod = _server()
    store = CommandStore(root=cmds)
    # The session's working folder (agent.cwd) is where template refs resolve.
    out = server_mod._handle_slash(_SlashAgent(cwd=str(home)), "/ctx go", store)
    assert NOTES in out.expand_to
    other = tmp_path / "other"
    other.mkdir()
    out = server_mod._handle_slash(_SlashAgent(cwd=str(other)), "/ctx go", store)
    assert NOTES not in out.expand_to
    # ...and [auto] trusted_dirs widen it.
    agent = _SlashAgent(cwd=str(other))
    agent.config.auto.trusted_dirs = [str(home)]
    (cmds / "abs.md").write_text(f"See @{home / 'notes.md'}\n", encoding="utf-8")
    assert NOTES in server_mod._handle_slash(agent, "/abs", store).expand_to
    # ...while [auto] protected_paths still win.
    agent.config.auto.protected_paths = ["notes.md"]
    assert NOTES not in server_mod._handle_slash(agent, "/abs", store).expand_to


def test_handle_slash_unbalanced_quote_does_not_raise(cmds: Path) -> None:
    server_mod = _server()
    out = server_mod._handle_slash(_SlashAgent(), '/q "two words tail', CommandStore(root=cmds))
    assert out.handled is False
    assert out.expand_to == 'first="two second=words all="two words tail'


@pytest.mark.parametrize(
    "exc", [ValueError("No closing quotation"), PermissionError(13, "Permission denied")]
)
def test_handle_slash_expand_error_is_a_message(
    cmds: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    server_mod = _server()
    store = CommandStore(root=cmds)

    def boom(*_a, **_k):
        raise exc

    monkeypatch.setattr(store, "expand", boom)
    out = server_mod._handle_slash(_SlashAgent(), '/q "x', store)
    assert out.handled is True
    assert "can't run `/q`" in out.text and str(exc) in out.text


def test_handle_slash_undecodable_command_file_is_a_message(tmp_path: Path) -> None:
    server_mod = _server()
    d = tmp_path / "badcmds"
    d.mkdir()
    (d / "bad.md").write_bytes(b"\xff\xfe\x81 not utf-8")
    out = server_mod._handle_slash(_SlashAgent(), "/bad x", CommandStore(root=d))
    assert out.handled is True
    assert "can't run `/bad`" in out.text and "bad.md" in out.text


def test_expand_for_agent_uses_the_session_scope(tmp_path: Path, home: Path, cmds: Path) -> None:
    """The one helper both chat surfaces expand through: the agent's folder, its
    [auto] trusted_dirs (what /add-dir extends) and its protected_paths."""
    store = CommandStore(root=cmds)
    (cmds / "abs.md").write_text(f"See @{home / 'notes.md'}\n", encoding="utf-8")
    assert NOTES in store.expand_for_agent(_SlashAgent(cwd=str(home)), "ctx", "go")
    other = tmp_path / "other"
    other.mkdir()
    agent = _SlashAgent(cwd=str(other))
    assert NOTES not in store.expand_for_agent(agent, "abs")
    agent.config.auto.trusted_dirs = [str(home)]
    assert NOTES in store.expand_for_agent(agent, "abs")
    agent.config.auto.protected_paths = ["*.md"]
    assert NOTES not in store.expand_for_agent(agent, "abs")
    assert store.expand_for_agent(agent, "nosuch") is None


# ---- CLI REPL slash path (same guarded expansion as the web) ----------------


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch):
    """The REPL's `_dispatch_slash`, with its console captured as text (`.out()`)."""
    import io
    from types import SimpleNamespace

    from rich.console import Console

    import evi.apps.cli.main as cli_main

    buf = io.StringIO()
    monkeypatch.setattr(cli_main, "console", Console(file=buf, width=200, color_system=None))
    return SimpleNamespace(_dispatch_slash=cli_main._dispatch_slash, out=buf.getvalue)


def test_cli_dispatch_slash_uses_the_session_scope(
    tmp_path: Path, home: Path, cmds: Path, cli
) -> None:
    """The REPL used to expand with no scope: its session folder (`evi chat
    --cwd`), [auto] trusted_dirs / `/add-dir` and protected_paths were ignored."""
    store = CommandStore(root=cmds)
    (cmds / "abs.md").write_text(f"See @{home / 'notes.md'}\n", encoding="utf-8")
    (cmds / "sec.md").write_text("Read @k.secret\n", encoding="utf-8")
    (cmds / "projnotes.md").write_text("Context: @notes.md\n", encoding="utf-8")  # /ctx: builtin
    (home / "k.secret").write_text("SECRET-VAL", encoding="utf-8")
    # agent.cwd is where refs resolve, even before the first turn sets the ContextVar.
    agent = _SlashAgent(cwd=str(home))
    assert NOTES in cli._dispatch_slash("/projnotes", agent, store)
    assert "SECRET-VAL" in cli._dispatch_slash("/sec", agent, store)
    agent.config.auto.protected_paths = ["*.secret"]  # the user's own patterns hold
    assert cli._dispatch_slash("/sec", agent, store) == "Read @k.secret"
    # Outside the folder only after /add-dir (or [auto] trusted_dirs).
    other = tmp_path / "other"
    other.mkdir()
    agent = _SlashAgent(cwd=str(other))
    assert NOTES not in cli._dispatch_slash("/abs", agent, store)
    assert cli._dispatch_slash(f"/add-dir {home}", agent, store) == "continue"
    assert NOTES in cli._dispatch_slash("/abs", agent, store)
    # Args stay text and bad quoting falls back to a whitespace split, as on the web.
    out = cli._dispatch_slash("/explain @~/.ssh/id_ed25519", agent, store)
    _no_secret(out)
    assert out == "Explain this: @~/.ssh/id_ed25519"
    assert cli._dispatch_slash('/q "two words tail', agent, store) == (
        'first="two second=words all="two words tail'
    )


def test_cli_and_web_slash_expand_alike(tmp_path: Path, home: Path, cmds: Path, cli) -> None:
    server_mod = _server()
    store = CommandStore(root=cmds)
    (cmds / "abs.md").write_text(f"See @{home / 'notes.md'} for $ARGUMENTS\n", encoding="utf-8")
    agent = _SlashAgent(cwd=str(tmp_path))
    agent.config.auto.trusted_dirs = [str(home)]
    for line in ("/abs x", "/explain @~/.evi/config.toml", '/q "a b'):
        assert cli._dispatch_slash(line, agent, store) == (
            server_mod._handle_slash(agent, line, store).expand_to
        )


def test_cli_dispatch_slash_undecodable_command_file_is_a_message(
    broken_cmds: Path, cli
) -> None:
    store = CommandStore(root=broken_cmds)
    agent = _SlashAgent()
    assert cli._dispatch_slash("/zz_bad x", agent, store) == "continue"
    assert "can't run /zz_bad: zz_bad.md: not UTF-8 text" in cli.out()
    # The broken sibling doesn't break the other commands.
    assert cli._dispatch_slash("/explain x", agent, store) == "Explain this: x"
    assert cli._dispatch_slash("/nosuch", agent, store) == "continue"
    assert "unknown command: /nosuch" in cli.out()


@pytest.mark.parametrize(
    "exc", [ValueError("No closing quotation"), PermissionError(13, "Permission denied")]
)
def test_cli_dispatch_slash_expand_error_is_a_message(
    cmds: Path, monkeypatch: pytest.MonkeyPatch, cli, exc: Exception
) -> None:
    store = CommandStore(root=cmds)

    def boom(*_a, **_k):
        raise exc

    monkeypatch.setattr(store, "expand", boom)
    assert cli._dispatch_slash('/q "x', _SlashAgent(), store) == "continue"
    assert "can't run /q:" in cli.out() and str(exc) in cli.out()


# ---- one unreadable command file doesn't break the others -------------------


@pytest.fixture
def broken_cmds(cmds: Path) -> Path:
    """`cmds` plus a non-UTF-8 command and a directory that looks like one."""
    (cmds / "zz_bad.md").write_bytes(b"\xff\xfe\x81 not utf-8")
    (cmds / "trap.md").mkdir()
    return cmds


def test_list_tolerates_unreadable_command_files(broken_cmds: Path) -> None:
    entries = {e.name: e for e in CommandStore(root=broken_cmds).list()}
    assert "trap" not in entries  # a directory isn't a command
    assert entries["zz_bad"].summary == "(unreadable: not UTF-8 text)"
    assert entries["explain"].summary == "Explain this: $ARGUMENTS"


def test_handle_slash_with_an_unreadable_sibling(broken_cmds: Path) -> None:
    server_mod = _server()
    store = CommandStore(root=broken_cmds)
    agent = _SlashAgent()
    out = server_mod._handle_slash(agent, "/explain x", store)
    assert out.handled is False and out.expand_to == "Explain this: x"
    out = server_mod._handle_slash(agent, "/nosuch x", store)
    assert out.handled is True and out.text.startswith("unknown command: `/nosuch`")
    out = server_mod._handle_slash(agent, "/help", store)
    assert "`/explain`" in out.text and "`/zz_bad` — (unreadable: not UTF-8 text)" in out.text
    out = server_mod._handle_slash(agent, "/zz_bad x", store)
    assert out.handled is True and "can't run `/zz_bad`" in out.text


def test_mcp_prompt_list_tolerates_unreadable_command_files(
    broken_cmds: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(commands_mod, "COMMANDS_DIR", broken_cmds)
    names = {s["name"] for s in publish.command_prompt_specs()}
    assert {"explain", "zz_bad"} <= names and "trap" not in names
    assert publish.expand_command_prompt("explain", {"args": "y"}) == "Explain this: y"


@pytest.fixture
def chat_client(cmds: Path, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    server_mod = _server()
    from fastapi.testclient import TestClient

    import evi.sdk.builder as builder_mod

    agent = _SlashAgent(cwd=str(home))
    monkeypatch.setattr(builder_mod, "build_agent", lambda *_, **__: agent)
    monkeypatch.setattr(server_mod, "make_client", lambda *_: None)
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path)
    monkeypatch.setattr(server_mod, "ensure_dirs", lambda: None)
    monkeypatch.setattr(commands_mod, "COMMANDS_DIR", cmds)  # create_app's CommandStore()
    return TestClient(server_mod.create_app()), agent


def _kinds(body: str) -> list[str]:
    import json

    return [
        json.loads(line[len("data:"):])["kind"]
        for line in body.splitlines()
        if line.startswith("data:") and line[len("data:"):].strip()
    ]


def test_api_chat_unbalanced_quote_is_not_a_500(chat_client) -> None:
    client, agent = chat_client
    r = client.post("/api/chat", json={"session_id": "q1", "message": '/q "two words tail'})
    assert r.status_code == 200
    assert "Done" in _kinds(r.text)
    assert agent.messages == ['first="two second=words all="two words tail']


def test_api_chat_with_broken_command_files_is_not_a_500(chat_client, cmds: Path) -> None:
    client, agent = chat_client
    (cmds / "zz_bad.md").write_bytes(b"\xff\xfe\x81 not utf-8")
    (cmds / "trap.md").mkdir()
    r = client.post("/api/chat", json={"session_id": "b1", "message": "/explain it"})
    assert r.status_code == 200
    assert agent.messages == ["Explain this: it"]
    r = client.post("/api/chat", json={"session_id": "b1", "message": "/help"})
    assert r.status_code == 200 and "zz_bad" in r.text


def test_api_chat_argument_ref_not_inlined(chat_client) -> None:
    client, agent = chat_client
    r = client.post(
        "/api/chat", json={"session_id": "q2", "message": "/explain @~/.ssh/id_ed25519"}
    )
    assert r.status_code == 200
    assert agent.messages == ["Explain this: @~/.ssh/id_ed25519"]
    _no_secret(r.text)
