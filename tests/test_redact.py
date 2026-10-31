"""Secret redaction (evi.redact): the token formats it knows by shape (also
right after a JSON escape, and PEM keys without eating code or JSON), the
known values it reads from this machine (env, config.toml, the active profile,
backends/peers/users/mcp/routines.json, registered sources), the minimum
length that keeps short values out, and the API later items build on
(Redactor counts and tool-call arguments, redact_obj, extra values).

Every source path points into tmp_path and every secret-named env var of the
test process is cleared first, so nothing here reads the real ~/.evi or
depends on the developer's environment.
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

import pytest

import evi.config as config_mod
import evi.profiles as profiles_mod
from evi import redact
from evi.redact import MARKER_RE, MIN_KNOWN_LEN, Redactor, redact_obj
from evi.transcripts import TranscriptStore

# A key with no recognisable shape: only the known-value set can catch it.
SHAPELESS = "Zq8vN2kLp4Xr7Tw1Ys5M"


@pytest.fixture(autouse=True)
def sources(monkeypatch, tmp_path) -> Path:
    """eVi's secret-holding files under tmp_path (none exist yet), no active
    profile, and no secret-named env var, with the known-value set rebuilt."""
    home = tmp_path / "evi-home"
    home.mkdir()
    for name in list(os.environ):
        if redact._SECRET_NAME.search(name):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("EVI_PROFILE", raising=False)
    monkeypatch.setattr(config_mod, "HOME", home)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", home / "config.toml")
    monkeypatch.setattr(config_mod, "BACKENDS_PATH", home / "backends.json")
    monkeypatch.setattr(config_mod, "PEERS_PATH", home / "peers.json")
    monkeypatch.setattr(config_mod, "USERS_PATH", home / "users.json")
    monkeypatch.setattr(config_mod, "MCP_CONFIG_PATH", home / "mcp.json")
    monkeypatch.setattr(profiles_mod, "PROFILES_DIR", home / "profiles")
    monkeypatch.setattr(redact, "_sources", {})
    redact.refresh()
    yield home
    redact.refresh()


# ---- token formats ---------------------------------------------------------------

# Each fake token is split, so a secret scanner (GitHub push protection) never
# takes the fixture for a leaked credential; the test sees the joined string.
FORMATS = [
    ("sk-ABCDEFGH12345678", "api_key"),
    ("sk-proj-" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7" * 2, "api_key"),
    ("sk-ant-api03-" + "x7Y2z9QwErTy" * 8, "api_key"),
    ("gsk_" + "a1B2c3D4e5" * 5, "api_key"),
    ("ghp_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345", "github_token"),
    ("ghs_" + "16C7e42F292c6912E7710c838347Ae178B4a", "github_token"),
    ("github_pat_" + "11ABCDEFG0123456789_abcdefghijKLMNOP", "github_token"),
    ("glpat-" + "abcdefghij1234567890", "gitlab_token"),
    ("AKIA" + "IOSFODNN7EXAMPLE", "aws_access_key"),
    ("ASIA" + "Y34FZKBOKMUTVV7A", "aws_access_key"),
    ("xoxb-" + "123456789012-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx", "slack_token"),
    ("xoxp-" + "1234567890-abcdefghij", "slack_token"),
    ("AIza" + "SyA1234567890abcdefghijklmnopqrstuv", "google_api_key"),
    ("hf_" + "abcdefghijklmnopqrstuvwxyz123456", "hf_token"),
    ("npm_" + "aB3dE6gH9j" * 4, "npm_token"),
    ("sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc", "stripe_key"),
    ("eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6y", "jwt"),
]


@pytest.mark.parametrize("token,kind", FORMATS, ids=[k + ":" + t[:8] for t, k in FORMATS])
def test_common_token_formats_are_redacted(token, kind) -> None:
    out = redact.redact(f"the tool said {token}, then stopped")
    assert token not in out
    assert out == f"the tool said [REDACTED:{kind}], then stopped"


def test_a_token_is_redacted_wherever_it_sits() -> None:
    key = "sk-ABCDEFGH12345678"
    for text in (key, f"KEY={key}", f'"{key}"', f"({key})", f"{key}\n", f"export X='{key}'"):
        out = redact.redact(text)
        assert key not in out and "[REDACTED:api_key]" in out, text


def test_authorization_credentials_keep_the_header_and_scheme() -> None:
    assert redact.redact("Authorization: Bearer abc.def-ghi") == (
        "Authorization: Bearer [REDACTED:bearer]"
    )
    assert redact.redact('-H "authorization: basic dXNlcjpwYXNzd29yZA=="') == (
        '-H "authorization: basic [REDACTED:bearer]"'
    )
    assert redact.redact('{"Authorization": "Bearer opaque-token-value-1"}') == (
        '{"Authorization": "Bearer [REDACTED:bearer]"}'
    )
    assert redact.redact("curl -H 'X: y' Bearer 9f8e7d6c5b4a39281706f5e4") == (
        "curl -H 'X: y' Bearer [REDACTED:bearer]"
    )
    # A JWT in a header is named for what it is.
    jwt = FORMATS[-1][0]
    assert redact.redact(f"Authorization: Bearer {jwt}") == (
        "Authorization: Bearer [REDACTED:jwt]"
    )


def test_private_key_blocks_are_redacted_whole_even_when_cut_off() -> None:
    pem = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ\n"
        "AAAAC3NzaC1lZDI1NTE5AAAAIH5vdGFyZWFsa2V5\n"
        "-----END OPENSSH PRIVATE KEY-----"
    )
    assert redact.redact(f"cat id_ed25519\n{pem}\n$ ") == (
        "cat id_ed25519\n[REDACTED:private_key]\n$ "
    )
    cut = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA1234567890abc\nMIIEfg[output truncated]"
    out = redact.redact(cut)
    assert "MIIE" not in out and out == "[REDACTED:private_key][output truncated]"
    # Encrypted (header fields), flattened onto one line (`echo $KEY`).
    enc = ("-----BEGIN RSA PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\n"
           "DEK-Info: AES-128-CBC,3F17F5316E2BAC89\n\nhYzqxXwJ0aGZbLLdHJ8a1Q2w3E4r5T6y\n"
           "-----END RSA PRIVATE KEY-----")
    assert redact.redact(f"$ cat k\n{enc}\n") == "$ cat k\n[REDACTED:private_key]\n"
    assert redact.redact(f"echo: {pem.replace(chr(10), ' ')} done") == (
        "echo: [REDACTED:private_key] done"
    )


def test_a_private_key_inside_json_is_redacted_and_the_json_stays_valid() -> None:
    pem = ("-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n"
           "BKcwggSjAgEAAoIBAQC7\n-----END PRIVATE KEY-----\n")
    for args in (json.dumps({"path": "k.pem", "content": pem}),
                 json.dumps({"path": "k.py", "content": f"KEY = {json.dumps(pem)}\n"}),
                 json.dumps({"content": pem[:60]})):  # cut off mid-body
        out = redact.redact(args)
        assert "MIIEvQIBADANBgkq" not in out, args
        assert "[REDACTED:private_key]" in json.loads(out)["content"]


# A key body: 9 lines of base64, none of them anything but a key's.
_BODY = base64.b64encode(bytes((i * 37 + 11) % 256 for i in range(432))).decode()
_BODY_LINES = [_BODY[i:i + 64] for i in range(0, len(_BODY), 64)]


def _key(kind="RSA PRIVATE KEY", headers=(), *, blank=False, sum_line=False,
         indent="", nl="\n") -> str:
    """A key block whose lines after the first are `indent`ed and end `nl`."""
    lines = [f"-----BEGIN {kind}-----", *headers, *([""] if blank else []),
             *_BODY_LINES, *(["=AbCd"] if sum_line else []), f"-----END {kind}-----"]
    return (nl + indent).join(lines)


_PGP = "PGP PRIVATE KEY BLOCK"

# (where the key sits, with {} for it; the key). The key is all that goes.
KEY_LAYOUTS = {
    "flush left": ("$ cat id_rsa\n{}\n$ ", _key()),
    "yaml block": ("tls:\n  key: |\n    {}\n  cert: tls.crt\n", _key(indent="    ")),
    "compose env (8)": ("services:\n  app:\n    environment:\n      KEY: |\n        {}\n",
                        _key(indent=" " * 8)),
    "python triple-quoted": ('def key():\n    return """\n    {}\n    """\n', _key(indent="    ")),
    "markdown code block": ("Put this in `id_rsa`:\n\n    {}\n\nThen run ssh.\n",
                            _key("OPENSSH PRIVATE KEY", indent="    ")),
    "tab indented": ("key:\n\t{}\n", _key("EC PRIVATE KEY", indent="\t")),
    "crlf indented": ("a:\r\n  {}\r\nb\r\n", _key(indent="  ", nl="\r\n")),
    "blank line after the header": ("{}\n", _key(blank=True)),
    "pgp, no headers": ("{}\n", _key(_PGP, blank=True, sum_line=True)),
    "pgp, version and comment": ("gpg> {}\n", _key(
        _PGP, ("Version: GnuPG v2.0.22 (GNU/Linux)", "Comment: GPGTools - https://gpgtools.org"),
        blank=True, sum_line=True)),
    "pgp, comment with a tab": ("{}\n", _key(
        _PGP, ("Comment: User-ID:\tAlice <alice@example.com>",), blank=True, sum_line=True)),
    "pgp in yaml": ("signing:\n  key: |\n    {}\n", _key(_PGP, blank=True, sum_line=True,
                                                         indent="    ")),
    "encrypted, flattened": ("echo: {} done", _key(
        headers=("Proc-Type: 4,ENCRYPTED", "DEK-Info: AES-128-CBC,3F17F5316E2BAC89"),
        nl=" ")),
}


@pytest.mark.parametrize("layout", KEY_LAYOUTS)
def test_a_private_key_is_redacted_however_it_is_laid_out(layout, tmp_path) -> None:
    # Indented (YAML, a Python string, Markdown), with a blank line or free-text
    # headers (PGP armour), as text, inside JSON (once or twice escaped), as
    # write_file arguments, and in the transcript a tool call writes.
    where, key = KEY_LAYOUTS[layout]
    text, want = where.format(key), where.format("[REDACTED:private_key]")
    assert redact.redact(text) == want
    for enc, dec in ((json.dumps, json.loads),
                     (lambda v: json.dumps(json.dumps(v)), lambda s: json.loads(json.loads(s)))):
        out = redact.redact(enc({"content": text}))
        assert dec(out) == {"content": want}  # still JSON, only the key gone
    args = json.dumps({"path": "deploy/values.yaml", "content": text})
    assert json.loads(Redactor().args(args)) == {"path": "deploy/values.yaml", "content": want}

    store = TranscriptStore(root=tmp_path / "transcripts")
    store.write_message("s", "assistant", "", tool_calls=[{
        "id": "c1", "type": "function",
        "function": {"name": "write_file", "arguments": args}}])
    store.write_message("s", "tool", text, tool_name="read_file", tool_call_id="c1")
    [path] = (tmp_path / "transcripts").rglob("s.jsonl")
    raw = path.read_text("utf-8")
    assert not [line for line in _BODY_LINES if line in raw]
    call, result = map(json.loads, raw.splitlines())
    assert json.loads(call["tool_calls"][0]["function"]["arguments"])["content"] == want
    assert result["content"] == want


@pytest.mark.parametrize("content", [
    'grep -rn "-----BEGIN RSA PRIVATE KEY-----" .',
    'PEM_HEADER = "-----BEGIN PRIVATE KEY-----\\n"\n',
    'assert pem.startswith("-----BEGIN RSA PRIVATE KEY-----")\n',
    # Code between a header and a footer constant is code.
    'BEGIN = "-----BEGIN RSA PRIVATE KEY-----"\n\n'
    'def load(path):\n    """Load the key."""\n    return open(path).read()\n\n'
    'END = "-----END RSA PRIVATE KEY-----"\n',
    "Your file must start with -----BEGIN PRIVATE KEY-----\n"
    "and then the base64 body follows on the next lines, then the footer.",
    # Indented, or after a blank line, a template is still not a key.
    "    -----BEGIN RSA PRIVATE KEY-----\n    ...\n    -----END RSA PRIVATE KEY-----\n",
    "Paste it after\n-----BEGIN PRIVATE KEY-----\n\n    (the base64 body goes here)\n",
])
def test_a_pem_header_that_is_not_a_key_is_left_alone(content) -> None:
    # As text, and inside JSON tool-call arguments (where a header constant
    # sits right before an escaped quote: eating that backslash broke JSON).
    assert redact.redact(content) == content
    args = json.dumps({"command": content})
    assert redact.redact(args) == args
    assert Redactor().args(args) is args


def test_private_key_scanning_is_linear() -> None:
    # Each BEGIN with no END used to scan to the end of the text: quadratic,
    # ~15s for a 256 KiB grep listing of key headers, inside a transcript write.
    for line in ("-----BEGIN RSA PRIVATE KEY-----\n", "x.pem:1:-----BEGIN RSA PRIVATE KEY-----!\n"):
        text = line * 8000
        t0 = time.perf_counter()
        assert redact.redact(text) == text
        assert time.perf_counter() - t0 < 3.0
    # A header value is free text, but stops before the next "-----": one
    # that ran to the end of the line made each BEGIN on it rescan the rest.
    text = "-----BEGIN RSA PRIVATE KEY----- X: " * 8000
    t0 = time.perf_counter()
    redact.redact(text)
    assert time.perf_counter() - t0 < 3.0


@pytest.mark.parametrize("token,kind", FORMATS, ids=[k + ":" + t[:8] for t, k in FORMATS])
def test_a_token_right_after_an_escape_is_redacted(token, kind) -> None:
    # In JSON or a repr, a token at the start of a line follows `\n`: the `n`
    # must not count as part of the token. JSON stays JSON.
    for esc in ("\n", "\t", "\r", "é"):
        for enc in (json.dumps({"c": "x" + esc + token}), json.dumps(["x" + esc + token])):
            out = redact.redact(enc)
            assert token not in out, (esc, enc)
            assert f"[REDACTED:{kind}]" in json.dumps(json.loads(out))
        assert token not in redact.redact(repr("x" + esc + token))
    out = redact.redact(json.dumps({"h": "curl -H\nBearer 9f8e7d6c5b4a39281706f5e4"}))
    assert json.loads(out) == {"h": "curl -H\nBearer [REDACTED:bearer]"}


@pytest.mark.parametrize("text", [
    "a task-management-system-2024 plan",       # sk- inside a word
    "pip install sk-learn-pipeline-runner",      # sk- with no digit
    'KEY_TYPE = b"sk-ecdsa-sha2-nistp256@openssh.com"',  # an SSH key type
    "sk-short1 and sk-abc123",                   # too short
    "Use a Bearer token. Bearer authentication is the norm.",
    'headers = {"Authorization": f"Bearer {token}"}',
    'curl -H "Authorization: Bearer $OPENAI_API_KEY"',
    "xoxo-gossip-girl-season-two",
    "AKIA is the prefix; AKIAIOSFODNN7EXAMPLE2 is too long",
    "ghp_tooShort and gho_alsoShort1",
    "hf_hub_download and hf_transfer",
    "the eyJ prefix starts every JWT",
])
def test_ordinary_text_is_left_alone(text) -> None:
    assert redact.redact(text) == text


# ---- known values ------------------------------------------------------------------


def test_an_env_api_key_is_redacted_by_value_and_named(monkeypatch) -> None:
    monkeypatch.setenv("ACME_API_KEY", SHAPELESS)
    monkeypatch.setenv("ACME_TOKEN", "t0k3n-abcdefghij")
    monkeypatch.setenv("ACME_CLIENT_SECRET", "s3cr3t/value+plus=")
    monkeypatch.setenv("ACME_PASSWORD", "correct-horse-battery-9")
    text = f"{SHAPELESS} t0k3n-abcdefghij s3cr3t/value+plus= correct-horse-battery-9"
    assert redact.redact(text) == (
        "[REDACTED:ACME_API_KEY] [REDACTED:ACME_TOKEN] "
        "[REDACTED:ACME_CLIENT_SECRET] [REDACTED:ACME_PASSWORD]"
    )


def test_env_vars_without_a_secret_name_are_not_known_values(monkeypatch) -> None:
    monkeypatch.setenv("ACME_SETTING", SHAPELESS)
    monkeypatch.setenv("MAX_TOKENS", "409640964096")
    monkeypatch.setenv("ACME_KEYBOARD", "us-international")
    text = f"{SHAPELESS} 409640964096 us-international"
    assert redact.redact(text) == text


def test_values_shorter_than_the_minimum_are_not_redacted(monkeypatch) -> None:
    short = "abcdef12345"[: MIN_KNOWN_LEN - 1]
    exact = "abcdefghij12"[:MIN_KNOWN_LEN]
    assert len(short) == MIN_KNOWN_LEN - 1 and len(exact) == MIN_KNOWN_LEN
    monkeypatch.setenv("SHORT_TOKEN", short)
    monkeypatch.setenv("EXACT_TOKEN", exact)
    monkeypatch.setenv("TINY_KEY", "1")
    out = redact.redact(f"[{short}] [{exact}] port 1")
    assert out == "[" + short + "] [[REDACTED:EXACT_TOKEN]] port 1"
    # The same floor holds for values a caller passes in.
    assert redact.redact(f"{short} {exact}", extra=[short, exact]) == (
        f"{short} [REDACTED:EXACT_TOKEN]"
    )
    monkeypatch.delenv("EXACT_TOKEN")
    assert redact.redact(f"{short} {exact}", extra=[short, exact]) == (
        f"{short} [REDACTED:secret]"
    )


def test_placeholders_references_and_key_file_paths_are_not_known_values(
    monkeypatch, tmp_path
) -> None:
    key_file = tmp_path / "id_ed25519_signing"
    key_file.write_text("not read", encoding="utf-8")
    monkeypatch.setenv("LOCAL_API_KEY", "sk-no-key-required")
    monkeypatch.setenv("MASKED_TOKEN", "****************")
    monkeypatch.setenv("REF_API_KEY", "env:OPENAI_API_KEY")
    monkeypatch.setenv("SIGNING_KEY", str(key_file))
    monkeypatch.setenv("PHRASE_SECRET", "two words here")
    monkeypatch.setenv("SHARE_KEY", "//fileserver/keys/deploy")  # never stat'ed
    text = (f"sk-no-key-required **************** env:OPENAI_API_KEY {key_file} "
            "two words here //fileserver/keys/deploy")
    assert redact.redact(text) == text


def test_config_secret_fields_are_known_values_and_follow_a_save(monkeypatch, tmp_path) -> None:
    from evi.config import Config

    home = config_mod.CONFIG_PATH.parent
    config_mod.CONFIG_PATH.write_text(
        '[llm]\napi_key = "cfg-llm-key-0123456789"\nmax_tokens = 4096\n'
        '[web]\nauth_token = "web-token-abcdef0123"\n'
        '[channels]\ntelegram_token = "123456789:AAH-telegram-bot-token"\n'
        '[google]\nclient_secrets_path = "client-secrets-file.json"\n',
        encoding="utf-8",
    )
    out = redact.redact(
        "cfg-llm-key-0123456789 web-token-abcdef0123 123456789:AAH-telegram-bot-token "
        "client-secrets-file.json"
    )
    assert out == (
        "[REDACTED:llm.api_key] [REDACTED:web.auth_token] "
        "[REDACTED:channels.telegram_token] client-secrets-file.json"
    )

    # A save from any surface is picked up with no refresh() call.
    monkeypatch.setattr(config_mod, "ensure_dirs", lambda: None)
    monkeypatch.chdir(tmp_path)
    cfg = Config.load()
    cfg.llm.api_key = "rotated-llm-key-9876543210-longer"
    cfg.save()
    assert (home / "config.toml").read_text(encoding="utf-8").count("rotated-llm-key") == 1
    assert redact.redact("rotated-llm-key-9876543210-longer cfg-llm-key-0123456789") == (
        "[REDACTED:llm.api_key] cfg-llm-key-0123456789"
    )


def test_the_active_profile_is_a_source(monkeypatch) -> None:
    prof = profiles_mod.PROFILES_DIR
    prof.mkdir()
    (prof / "work.toml").write_text('[llm]\napi_key = "profile-key-abcdef123"\n', encoding="utf-8")
    assert redact.redact("profile-key-abcdef123") == "profile-key-abcdef123"
    monkeypatch.setenv("EVI_PROFILE", "work")
    assert redact.redact("profile-key-abcdef123") == "[REDACTED:llm.api_key]"


def test_evis_json_credential_files_are_sources() -> None:
    config_mod.PEERS_PATH.write_text(json.dumps([
        {"name": "gpu", "url": "http://gpu-box:8473", "token": "peer-token-0123456789"},
    ]), encoding="utf-8")
    config_mod.USERS_PATH.write_text(json.dumps([
        {"name": "alice", "token": "user-token-abcdefghij1"},
    ]), encoding="utf-8")
    config_mod.BACKENDS_PATH.write_text(json.dumps([
        {"name": "cloud", "api_key": "backend-literal-key-42"},
        {"name": "openai", "api_key": "env:OPENAI_API_KEY"},
    ]), encoding="utf-8")
    config_mod.MCP_CONFIG_PATH.write_text(json.dumps({"mcpServers": {
        "github": {"command": "gh-mcp", "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "mcp-env-token-777777"}},
        "remote": {"url": "https://x.invalid/mcp", "headers": {"Authorization": "Bearer hdr-cred-123456789"}},
    }}), encoding="utf-8")
    out = redact.redact(
        "peer-token-0123456789 user-token-abcdefghij1 backend-literal-key-42 "
        "env:OPENAI_API_KEY mcp-env-token-777777 hdr-cred-123456789 gpu-box alice"
    )
    assert out == (
        "[REDACTED:peers.token] [REDACTED:users.token] [REDACTED:backends.api_key] "
        "env:OPENAI_API_KEY [REDACTED:mcp.GITHUB_PERSONAL_ACCESS_TOKEN] "
        "[REDACTED:mcp.Authorization] gpu-box alice"
    )


def test_routine_tokens_are_known_values(sources) -> None:
    from evi import routines

    tok = routines.add("nightly", "triage", root=sources).token
    assert redact.redact(f"curl -X POST /api/routine/{tok}") == (
        "curl -X POST /api/routine/[REDACTED:routines.token]"
    )


def test_a_malformed_source_is_skipped_not_fatal(monkeypatch) -> None:
    config_mod.CONFIG_PATH.write_text("[llm\napi_key = ", encoding="utf-8")
    config_mod.PEERS_PATH.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("ACME_API_KEY", SHAPELESS)
    assert redact.redact(f"{SHAPELESS} sk-ABCDEFGH12345678") == (
        "[REDACTED:ACME_API_KEY] [REDACTED:api_key]"
    )


def test_a_source_nested_too_deep_is_skipped_not_fatal(monkeypatch, tmp_path) -> None:
    from evi.transcripts import TranscriptStore

    # Too deep to parse at all, and deep enough to parse but not to walk.
    config_mod.MCP_CONFIG_PATH.write_text("[" * 200_000 + "]" * 200_000, encoding="utf-8")
    config_mod.USERS_PATH.write_text('{"a": ' * 990 + "1" + "}" * 990, encoding="utf-8")
    config_mod.PEERS_PATH.write_text(json.dumps([{"token": "peer-token-0123456789"}]),
                                     encoding="utf-8")
    monkeypatch.setenv("ACME_API_KEY", SHAPELESS)
    assert redact.redact(f"{SHAPELESS} peer-token-0123456789 sk-ABCDEFGH12345678") == (
        "[REDACTED:ACME_API_KEY] [REDACTED:peers.token] [REDACTED:api_key]"
    )
    TranscriptStore(root=tmp_path / "t").write_message("s", "user", "still written")


def test_if_known_values_cannot_be_built_the_patterns_still_apply(monkeypatch) -> None:
    def boom(*a):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(redact, "_build", boom)
    monkeypatch.setenv("ACME_API_KEY", SHAPELESS)
    assert redact.redact(f"{SHAPELESS} sk-ABCDEFGH12345678") == f"{SHAPELESS} [REDACTED:api_key]"


def test_other_modules_can_register_a_source() -> None:
    held = {"v": "director-key-0123456789abcdef"}
    redact.register_source("director", lambda: [("director_key", held["v"]), ("x", "short")])
    redact.register_source("broken", lambda: 1 / 0)
    assert redact.redact(f"key {held['v']} short") == "key [REDACTED:director_key] short"
    # A source's values change where nothing here can see it: refresh() then.
    old, held["v"] = held["v"], "rotated-director-key-9876543210"
    redact.refresh()
    assert redact.redact(f"{old} {held['v']}") == f"{old} [REDACTED:director_key]"
    redact.register_source("director", None)
    assert redact.redact(held["v"]) == held["v"]


def test_a_known_value_is_found_inside_json_tool_arguments(monkeypatch) -> None:
    tricky = 'pa"ss\\w0rd-ünïcode-123'
    monkeypatch.setenv("DB_PASSWORD", tricky)
    for args in (json.dumps({"q": tricky}), json.dumps({"q": tricky}, ensure_ascii=False)):
        out = redact.redact(args)
        assert json.loads(out) == {"q": "[REDACTED:DB_PASSWORD]"}


def test_the_longest_known_value_wins(monkeypatch) -> None:
    monkeypatch.setenv("BASE_TOKEN", "abcdefghij0123")
    monkeypatch.setenv("LONG_TOKEN", "abcdefghij0123-extended")
    assert redact.redact("abcdefghij0123-extended abcdefghij0123") == (
        "[REDACTED:LONG_TOKEN] [REDACTED:BASE_TOKEN]"
    )


def test_a_known_value_takes_its_source_name_over_its_shape(monkeypatch) -> None:
    key = "sk-proj-Zx81kq0PLm4nVb7cXy2W"
    monkeypatch.setenv("OPENAI_API_KEY", key)
    assert redact.redact(f"echo {key}") == "echo [REDACTED:OPENAI_API_KEY]"
    assert Redactor(known=False)(f"echo {key}") == "echo [REDACTED:api_key]"


# ---- the API ---------------------------------------------------------------------


def test_redactor_counts_what_it_replaced(monkeypatch) -> None:
    monkeypatch.setenv("ACME_API_KEY", SHAPELESS)
    r = Redactor(extra=["caller-known-secret-1"])
    assert r("nothing here") == "nothing here" and r.total == 0
    r(f"{SHAPELESS} and {SHAPELESS} and ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345")
    r("caller-known-secret-1")
    assert r.counts == {"ACME_API_KEY": 2, "github_token": 1, "secret": 1}
    assert r.total == 4


def test_redact_obj_rewrites_strings_only() -> None:
    key = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    obj = {
        "content": f"token {key}",
        "n": 3, "ok": True, "none": None,
        key: "a key is never rewritten",
        "calls": [{"id": "call_0", "arguments": json.dumps({"t": key})}],
        "pair": (key, 1.5),
    }
    out = redact_obj(obj)
    assert out == {
        "content": "token [REDACTED:github_token]",
        "n": 3, "ok": True, "none": None,
        key: "a key is never rewritten",
        "calls": [{"id": "call_0", "arguments": '{"t": "[REDACTED:github_token]"}'}],
        "pair": ("[REDACTED:github_token]", 1.5),
    }
    assert obj["content"] == f"token {key}"  # a copy; the input is untouched


def test_tool_call_arguments_are_redacted_as_the_json_they_are(monkeypatch) -> None:
    monkeypatch.setenv("ACME_API_KEY", SHAPELESS)
    gh, aws = FORMATS[4][0], FORMATS[8][0]
    r = Redactor()
    # A token on its own line, a known value, a secret as a key, a header in
    # JSON inside a string (as text its quotes are escaped): all go, and the
    # result is JSON again.
    args = json.dumps({"text": f"CI tokens:\n{gh}\n{aws}\n", "env": {SHAPELESS: "k"},
                       "headers": json.dumps({"Authorization": "Bearer opaque-cred-1"})})
    assert json.loads(r.args(args)) == {
        "text": "CI tokens:\n[REDACTED:github_token]\n[REDACTED:aws_access_key]\n",
        "env": {"[REDACTED:ACME_API_KEY]": "k"},
        "headers": '{"Authorization": "Bearer [REDACTED:bearer]"}',
    }
    assert r.counts == {"github_token": 1, "aws_access_key": 1, "ACME_API_KEY": 1, "bearer": 1}
    # Nothing to redact: the very same string, however it was formatted.
    clean = '{"path":"a.py",  "n": 1.50}'
    assert r.args(clean) is clean and r.total == 4
    # Not JSON (a model's broken call): redacted as text.
    assert r.args(f'{{"text": "{gh}') == '{"text": "[REDACTED:github_token]'
    assert r.args("") == "" and r.args(None) is None


def test_a_duplicate_keys_earlier_value_is_redacted_in_arguments(monkeypatch) -> None:
    """json.loads keeps only a duplicate key's last value, so the secret in the
    earlier one is never parsed: the text pass still finds it."""
    monkeypatch.setenv("ACME_API_KEY", SHAPELESS)
    r = Redactor()
    dup = '{"note": "' + SHAPELESS + '", "note": "x"}'
    out = r.args(dup)
    assert SHAPELESS not in out and r.counts == {"ACME_API_KEY": 1}
    assert out == '{"note": "[REDACTED:ACME_API_KEY]", "note": "x"}'
    json.loads(out)  # still JSON
    # With a hit the parse found too: re-serialised, the duplicate is gone.
    both = '{"a": "sk-ABCDEFGH12345678", "note": "' + SHAPELESS + '", "note": "x"}'
    out = Redactor().args(both)
    assert SHAPELESS not in out and "sk-ABCDEFGH" not in out
    assert json.loads(out) == {"a": "[REDACTED:api_key]", "note": "x"}


def test_a_numeric_known_value_in_arguments_is_redacted(monkeypatch) -> None:
    monkeypatch.setenv("PROBE_PIN_TOKEN", "4815162342108")
    r = Redactor()
    assert r.args('{"pin": 4815162342108}') == '{"pin": "[REDACTED:PROBE_PIN_TOKEN]"}'
    assert json.loads(r.args('{"v": [1, 4815162342108.0, {"n": 94815162342108}]}')) == {
        "v": [1, "[REDACTED:PROBE_PIN_TOKEN].0", {"n": "9[REDACTED:PROBE_PIN_TOKEN]"}]}
    assert r.counts == {"PROBE_PIN_TOKEN": 3}
    # Other numbers (and booleans) are left as they are, the string untouched.
    clean = '{"n": 4815162342107, "ok": true, "f": 1.50}'
    assert r.args(clean) is clean and r.total == 3


def test_a_header_beside_its_credential_in_arguments_is_redacted() -> None:
    """Parsed, the header name and its credential are separate strings, too
    short for the bare `Bearer` shape; as text, the header pattern sees both."""
    out = Redactor().args('{"Authorization": "Bearer abcdefgh1"}')
    assert out == '{"Authorization": "Bearer [REDACTED:bearer]"}'


def test_arguments_stay_json_when_the_text_pass_hits_json_syntax(monkeypatch) -> None:
    # A (contrived) known value that straddles a key and its value: as text
    # it matches, and replacing it breaks the JSON. The result is still JSON
    # (one string) and still has no secret.
    monkeypatch.setenv("ODD_TOKEN", 'abcdefgh":"ijklmn')
    out = Redactor().args('{"abcdefgh":"ijklmn"}')
    assert json.loads(out) == '{"[REDACTED:ODD_TOKEN]"}'


def test_tool_calls_keep_ids_types_and_names() -> None:
    gh = FORMATS[4][0]
    calls = [
        {"id": gh, "type": "function", "index": 0,  # an id is kept whatever it holds
         "function": {"name": "save_note", "arguments": json.dumps({"t": f"a\n{gh}"})}},
        {"id": "call_1", "type": "function",
         "function": {"name": "f", "arguments": {"t": gh}}},  # already parsed
        "odd",
    ]
    out = Redactor().tool_calls(calls)
    assert out[0]["id"] == gh and out[0]["type"] == "function"
    assert out[0]["index"] == 0 and out[0]["function"]["name"] == "save_note"
    assert json.loads(out[0]["function"]["arguments"]) == {"t": "a\n[REDACTED:github_token]"}
    assert out[1]["function"]["arguments"] == {"t": "[REDACTED:github_token]"}
    assert out[2] == "odd"
    assert calls[0]["function"]["arguments"] == json.dumps({"t": f"a\n{gh}"})  # a copy
    assert Redactor().tool_calls(None) is None


def test_extra_accepts_a_single_string_and_markers_are_recognisable() -> None:
    out = redact.redact("x caller-known-secret-1 y", extra="caller-known-secret-1")
    assert out == "x [REDACTED:secret] y"
    assert MARKER_RE.findall(out) == ["[REDACTED:secret]"]
    assert redact.redact(None) is None and redact.redact("") == ""


# ---- terminal colour codes ---------------------------------------------------------


@pytest.mark.parametrize("token,kind", FORMATS, ids=[k + ":" + t[:8] for t, k in FORMATS])
def test_a_token_right_after_a_terminal_colour_code_is_redacted(token, kind) -> None:
    # `grep --color=always` output, or a tool forcing colour: the code's last
    # letter must not count as part of the token. Raw, as JSON writes the ESC
    # (\u001b) and as a repr does (\x1b); JSON stays JSON.
    for code in ("\x1b[32m", "\x1b[01;31m\x1b[K", "\x1b[0m", "\x1b[1m"):
        text = f"hit: {code}{token}\x1b[m\x1b[K rest"
        assert redact.redact(text) == f"hit: {code}[REDACTED:{kind}]\x1b[m\x1b[K rest", code
        enc = json.dumps({"o": text})
        out = redact.redact(enc)
        assert token not in out
        assert json.loads(out) == {"o": f"hit: {code}[REDACTED:{kind}]\x1b[m\x1b[K rest"}
        assert token not in redact.redact(repr(text))


@pytest.mark.parametrize("text", [
    "[0mghp_tooShort",                # no ESC before the bracket: not a colour code
    "x[1msk-learn-pipeline-runner",   # and a word, not a key, after it
    "\x1b[31mtask-management-system-2024\x1b[0m",
])
def test_colour_codes_alone_redact_nothing(text) -> None:
    assert redact.redact(text) == text


# ---- known values change in place ---------------------------------------------------


def test_a_same_size_rotation_within_one_clock_tick_is_picked_up() -> None:
    """A rotated token of fixed length leaves the file's size as it was, and
    within one tick of the file system's clock its modification time too."""
    path = config_mod.CONFIG_PATH
    path.write_text('[web]\nauth_token = "first-token-0123456789"\n', encoding="utf-8")
    st = path.stat()
    assert redact.redact("first-token-0123456789") == "[REDACTED:web.auth_token]"
    path.write_text('[web]\nauth_token = "other-token-9876543210"\n', encoding="utf-8")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert path.stat().st_size == st.st_size and path.stat().st_mtime_ns == st.st_mtime_ns
    assert redact.redact("other-token-9876543210 first-token-0123456789") == (
        "[REDACTED:web.auth_token] first-token-0123456789"
    )


# ---- the director's attestation key -------------------------------------------------


@pytest.fixture
def director(monkeypatch, sources):
    """A director key in the file backend of this test's home (no keyring),
    in a process evi.attest reads it in: no agent marker, no eVi above it."""
    from evi import attest

    monkeypatch.setattr(attest, "_keyring", lambda: None)
    monkeypatch.setattr(attest, "_process_chain", lambda: [(1, ["python", "-m", "pytest"])])
    monkeypatch.delenv("EVI_AGENT", raising=False)
    monkeypatch.delenv("EVI_INTERACTIVE", raising=False)
    monkeypatch.setattr(redact, "_director_forms", set())
    return _write_director_key(os.urandom(32))


def _write_director_key(key: bytes) -> bytes:
    from evi import attest

    path = attest.key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(attest._FILE_MAGIC + key)
    return key


def _forms(key: bytes) -> list[str]:
    from evi import attest

    return [key.hex(), key.hex().upper(), base64.b64encode(key).decode(),
            base64.urlsafe_b64encode(key).decode().rstrip("="),
            base64.b64encode(attest._FILE_MAGIC + key).decode()]


def test_the_director_key_is_redacted_in_every_form_it_is_kept_or_printed(director) -> None:
    for form in _forms(director):
        assert redact.redact(f"key: {form}\n") == "key: [REDACTED:director_key]\n", form
    # In a transcript too (a tool that printed it).
    root = config_mod.HOME / "transcripts"
    TranscriptStore(root=root).write_message("s1", "tool", f"read: {director.hex()}", tool_name="x")
    raw = "".join(p.read_text(encoding="utf-8") for p in root.rglob("*.jsonl"))
    assert director.hex() not in raw and "[REDACTED:director_key]" in raw


def test_the_director_key_is_never_read_in_an_agents_process(director, monkeypatch) -> None:
    """Read through evi.attest only, so only where it reads the key: never
    where EVI_AGENT says an agent started this process."""
    monkeypatch.setenv("EVI_AGENT", "1")
    redact.refresh()
    assert redact.redact(director.hex()) == director.hex()
    assert redact._director_forms == set()


def test_a_director_key_read_before_the_repl_marks_itself_interactive_stays_redacted(
    director, monkeypatch
) -> None:
    redact.prime()
    monkeypatch.setenv("EVI_INTERACTIVE", "1")  # evi.attest reads no key from here on
    config_mod.CONFIG_PATH.write_text('[llm]\nmodel = "x"\n', encoding="utf-8")  # a rebuild
    assert redact.redact(director.hex()) == "[REDACTED:director_key]"
    # Unprimed, the same process could not have known it.
    monkeypatch.setattr(redact, "_director_forms", set())
    redact.refresh()
    assert redact.redact(director.hex()) == director.hex()


def test_a_rotated_director_key_file_is_read_again(director) -> None:
    assert redact.redact(director.hex()) == "[REDACTED:director_key]"
    new = _write_director_key(os.urandom(32))  # same size, maybe the same clock tick
    assert redact.redact(f"{new.hex()} {director.hex()}") == (
        "[REDACTED:director_key] [REDACTED:director_key]"  # the old one stays known
    )


def test_without_a_director_key_the_process_tree_is_not_walked(monkeypatch) -> None:
    from evi import attest

    monkeypatch.setattr(attest, "_keyring", lambda: None)
    monkeypatch.setattr(attest, "_process_chain",
                        lambda: pytest.fail("walked the process tree with no key to read"))
    assert not attest.key_path().exists()
    assert redact.redact("nothing here") == "nothing here"


def test_the_repl_primes_redaction_before_it_marks_itself_interactive(
    monkeypatch, tmp_path
) -> None:
    from tests.test_agent_cancel import _agent, _Client, _repl, _run

    cli_main, _reads = _repl(monkeypatch, tmp_path, [])
    seen: list[str | None] = []
    monkeypatch.setattr(redact, "prime", lambda: seen.append(os.environ.get("EVI_INTERACTIVE")))
    _run(cli_main, _agent(_Client([])))
    assert seen == ["0"]  # _repl's own value, before the REPL set "1"
