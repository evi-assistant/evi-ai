"""TTS must never put model text into a shell script or onto a command line.

Hotfix 1.0.53: the Windows voice used to build
``powershell -Command "...$s.Speak('<text>')"`` with only ASCII quotes
escaped, but PowerShell also treats U+2018/U+2019/U+201A/U+201B as single
quotes, so model output like ``x’);Start-Process calc;(’`` ran code. The text
now travels in environment variables (Windows) or stdin (say / espeak).

subprocess is always mocked — nothing here spawns a real TTS process.
"""

from __future__ import annotations

import os
import subprocess
import threading

import pytest

import evi.voice as voice_mod
from evi.voice import VoiceError, speak

# The whole PowerShell script for a single-chunk utterance: constants only.
_PS_SCRIPT_ONE_CHUNK = (
    "Add-Type -AssemblyName System.Speech;"
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
    "$s.Speak($env:EVI_TTS_TEXT_0);"
)

PAYLOADS = [
    "x\u2019);Start-Process calc;(\u2019",   # right single quote (the PoC)
    "x\u2018);Start-Process calc;(\u2018",   # left single quote
    "x\u201a);Start-Process calc;(\u201a",   # single low-9 quote
    "x\u201b);Start-Process calc;(\u201b",   # single high-reversed-9 quote
    "x');Start-Process calc;('",            # plain ASCII quote
    'x");Start-Process calc;("',            # double quote
    "x`);Start-Process calc",               # backtick escape
    "$(Start-Process calc)",                # subexpression
    "hello; Start-Process calc",            # statement separator
    "hello\nStart-Process calc",            # newline
    "@(Start-Process calc) | Out-Null",     # array subexpression + pipe
]


def _capture_run(monkeypatch: pytest.MonkeyPatch, backend: str) -> list[dict]:
    """Mock detect_backend + subprocess.run; return the list of captured calls."""
    calls: list[dict] = []
    monkeypatch.setattr(voice_mod, "detect_backend", lambda: backend)

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": list(cmd), **kwargs})
        return subprocess.CompletedProcess(cmd, returncode=0)

    def no_popen(*a, **kw):
        raise AssertionError("blocking speak must not use Popen")

    monkeypatch.setattr(voice_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(voice_mod.subprocess, "Popen", no_popen)
    return calls


def _capture_popen(monkeypatch: pytest.MonkeyPatch, backend: str) -> list[dict]:
    """Mock detect_backend + subprocess.Popen; stdin contents are read eagerly."""
    calls: list[dict] = []
    monkeypatch.setattr(voice_mod, "detect_backend", lambda: backend)

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            entry = {"cmd": list(cmd), **kwargs}
            stdin = kwargs.get("stdin")
            if stdin is not None and hasattr(stdin, "read"):
                entry["stdin_bytes"] = stdin.read()
            calls.append(entry)

    def no_run(*a, **kw):
        raise AssertionError("non-blocking speak must not use run")

    monkeypatch.setattr(voice_mod.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(voice_mod.subprocess, "run", no_run)
    return calls


def _env_chunks(env: dict) -> list[str]:
    """EVI_TTS_TEXT_0, _1, ... in order, up to the first missing index."""
    chunks = []
    while f"EVI_TTS_TEXT_{len(chunks)}" in env:
        chunks.append(env[f"EVI_TTS_TEXT_{len(chunks)}"])
    return chunks


def _ps_script(call: dict) -> str:
    cmd = call["cmd"]
    assert cmd[0] == voice_mod._powershell_exe()
    assert cmd[1:3] == ["-NoProfile", "-Command"]
    assert len(cmd) == 4
    return cmd[3]


# ---- Windows / PowerShell -------------------------------------------------


@pytest.mark.parametrize("payload", PAYLOADS)
def test_windows_payload_never_in_script(monkeypatch, payload):
    calls = _capture_run(monkeypatch, "windows")
    speak(payload, blocking=True)
    assert len(calls) == 1
    script = _ps_script(calls[0])
    # The script is the fixed constant — no model text at all.
    assert script == _PS_SCRIPT_ONE_CHUNK
    assert payload not in script and "Start-Process" not in script
    assert not set(script) & set("\u2018\u2019\u201a\u201b`'\"\n")
    # ...and the env var carries the text exactly.
    assert calls[0]["env"]["EVI_TTS_TEXT_0"] == payload
    assert calls[0].get("input") is None


@pytest.mark.parametrize("payload", PAYLOADS)
def test_windows_nonblocking_payload_never_in_script(monkeypatch, payload):
    calls = _capture_popen(monkeypatch, "windows")
    speak(payload, blocking=False)
    assert len(calls) == 1
    assert _ps_script(calls[0]) == _PS_SCRIPT_ONE_CHUNK
    assert calls[0]["env"]["EVI_TTS_TEXT_0"] == payload


def test_windows_typographic_apostrophe_passes_through(monkeypatch):
    calls = _capture_run(monkeypatch, "windows")
    speak("I\u2019m fine", blocking=True)
    assert calls[0]["env"]["EVI_TTS_TEXT_0"] == "I\u2019m fine"
    assert _ps_script(calls[0]) == _PS_SCRIPT_ONE_CHUNK


def test_windows_powershell_is_absolute_system32_path(monkeypatch, tmp_path):
    # A bare "powershell.exe" lets CreateProcess run a copy planted in the
    # current directory (e.g. a cloned repo) before System32.
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    calls = _capture_run(monkeypatch, "windows")
    speak("hello", blocking=True)
    exe = calls[0]["cmd"][0]
    assert exe == os.path.join(
        str(tmp_path), "System32", "WindowsPowerShell", "v1.0", "powershell.exe",
    )
    assert os.path.isabs(exe)


def test_windows_powershell_path_without_systemroot(monkeypatch):
    monkeypatch.delenv("SystemRoot", raising=False)
    calls = _capture_run(monkeypatch, "windows")
    speak("hello", blocking=True)
    exe = calls[0]["cmd"][0]
    assert exe.startswith("C:\\Windows")
    assert exe.endswith("powershell.exe") and exe != "powershell.exe"


def test_windows_env_inherits_parent_environment(monkeypatch):
    monkeypatch.setenv("EVI_TEST_INHERITED", "1")
    calls = _capture_run(monkeypatch, "windows")
    speak("hello", blocking=True)
    env = calls[0]["env"]
    assert env.get("EVI_TEST_INHERITED") == "1"


def test_windows_nul_is_dropped(monkeypatch):
    calls = _capture_run(monkeypatch, "windows")
    speak("a\x00b", blocking=True)
    assert calls[0]["env"]["EVI_TTS_TEXT_0"] == "ab"


def test_windows_long_text_is_chunked(monkeypatch):
    words = [f"w{i:05d}’" for i in range(6000)]   # ~48k chars with spaces
    text = " ".join(words)
    calls = _capture_run(monkeypatch, "windows")
    speak(text, blocking=True)
    assert len(calls) == 1  # one process speaks every chunk, in order
    script = _ps_script(calls[0])
    chunks = _env_chunks(calls[0]["env"])
    assert len(chunks) == 3
    assert "".join(chunks) == text
    assert script.endswith(
        "".join(f"$s.Speak($env:EVI_TTS_TEXT_{i});" for i in range(len(chunks)))
    )
    assert "w00001" not in script and "’" not in script
    for i, chunk in enumerate(chunks):
        assert len(chunk) <= voice_mod._PS_ENV_CHUNK
        # Broke before a space, not mid-word.
        assert i == 0 or chunk.startswith(" ")


def test_windows_chunks_fit_env_limit_with_astral_chars(monkeypatch):
    # No spaces (hard cut) and every char a surrogate pair in UTF-16.
    text = "😀" * 40_000
    calls = _capture_run(monkeypatch, "windows")
    speak(text, blocking=True)
    chunks = _env_chunks(calls[0]["env"])
    assert len(chunks) == 3
    assert "".join(chunks) == text
    for c in chunks:
        assert len(c.encode("utf-16-le")) // 2 <= 32_767


def test_chunk_text_roundtrip_and_bounds():
    assert voice_mod._chunk_text("short") == ["short"]
    text = "ab " * 10 + "x" * 25
    chunks = voice_mod._chunk_text(text, limit=8)
    assert "".join(chunks) == text
    assert all(0 < len(c) <= 8 for c in chunks)


@pytest.mark.parametrize("rate", [-10, -3, 0, 10])
def test_windows_rate_is_validated_int(monkeypatch, rate):
    calls = _capture_run(monkeypatch, "windows")
    speak("hello", rate=rate, blocking=True)
    assert f"$s.Rate = {rate};" in _ps_script(calls[0])


@pytest.mark.parametrize("rate", [140, 11, -11, 500])
def test_windows_out_of_range_rate_speaks_at_default(monkeypatch, rate):
    # The docs' `evi voice speak ... --rate 140` (a WPM value): PowerShell
    # always rejected Rate = 140 and spoke at the default rate. Keep that
    # instead of failing the whole call.
    calls = _capture_run(monkeypatch, "windows")
    speak("Build finished. All tests passed.", rate=rate, blocking=True)
    assert len(calls) == 1
    assert _ps_script(calls[0]) == _PS_SCRIPT_ONE_CHUNK
    assert calls[0]["env"]["EVI_TTS_TEXT_0"] == "Build finished. All tests passed."


class _SneakyInt(int):
    """An int whose text form is script — must never reach the command."""

    def __format__(self, spec):
        return "1;Start-Process calc"

    def __str__(self):
        return "1;Start-Process calc"

    __repr__ = __str__


@pytest.mark.parametrize(
    "bad", ["1;Start-Process calc", "3", 1.5, True, _SneakyInt(1), _SneakyInt(140)],
)
def test_windows_bad_rate_rejected_before_spawn(monkeypatch, bad):
    calls = _capture_run(monkeypatch, "windows")
    with pytest.raises(VoiceError, match="rate"):
        speak("hello", rate=bad, blocking=True)
    assert calls == []


# ---- macOS say / Linux espeak ---------------------------------------------


OPTION_LIKE = ["-o /tmp/evil.aiff hello", "--help", "-w /tmp/x.wav hi", "-"]


def _stdin_payload(text: str) -> bytes:
    # A trailing newline, as `echo text | espeak-ng --stdin` would send.
    return (text + "\n").encode("utf-8")


@pytest.mark.parametrize("text", OPTION_LIKE + PAYLOADS)
def test_say_reads_text_from_stdin(monkeypatch, text):
    calls = _capture_run(monkeypatch, "macos")
    speak(text, blocking=True)
    assert calls[0]["cmd"] == ["say", "-f", "-"]
    assert calls[0]["input"] == _stdin_payload(text)
    assert calls[0].get("env") is None


def test_say_rate(monkeypatch):
    calls = _capture_run(monkeypatch, "macos")
    speak("-hello", rate=200, blocking=True)
    assert calls[0]["cmd"] == ["say", "-r", "200", "-f", "-"]
    assert calls[0]["input"] == b"-hello\n"


@pytest.mark.parametrize("backend", ["espeak", "espeak-ng"])
@pytest.mark.parametrize("text", OPTION_LIKE + PAYLOADS)
def test_espeak_reads_text_from_stdin(monkeypatch, backend, text):
    calls = _capture_run(monkeypatch, backend)
    speak(text, rate=180, blocking=True)
    assert calls[0]["cmd"] == [backend, "-s", "180", "--stdin"]
    assert calls[0]["input"] == _stdin_payload(text)


@pytest.mark.parametrize(
    "text",
    ["Build finished", "All tests passed.", "I\u2019m fine", "Je suis pr\xeat\u2026"],
)
@pytest.mark.parametrize("blocking", [True, False])
def test_espeak_ng_stdin_keeps_last_character(monkeypatch, text, blocking):
    # espeak-ng 1.50-1.52 `--stdin` does `p_text[ix-1] = 0` after reading,
    # i.e. drops the last byte. It must be our newline, not the text's.
    if blocking:
        calls = _capture_run(monkeypatch, "espeak-ng")
        speak(text, blocking=True)
        payload = calls[0]["input"]
    else:
        calls = _capture_popen(monkeypatch, "espeak-ng")
        speak(text, blocking=False)
        payload = calls[0]["stdin_bytes"]
    assert payload.endswith(b"\n")
    assert payload[:-1].decode("utf-8") == text


@pytest.mark.parametrize("backend", ["macos", "espeak-ng"])
def test_nonblocking_stdin_carries_text(monkeypatch, backend):
    text = "-w /tmp/x.wav I\u2019m fine"
    calls = _capture_popen(monkeypatch, backend)
    speak(text, blocking=False)
    assert len(calls) == 1
    assert text not in calls[0]["cmd"]
    assert all(not arg.startswith("-w") for arg in calls[0]["cmd"])
    assert calls[0]["stdin_bytes"] == _stdin_payload(text)


@pytest.mark.parametrize("backend", ["macos", "espeak-ng", "windows"])
def test_lone_surrogate_is_dropped_not_raised(monkeypatch, backend):
    # JSON-decoded model output can carry a lone surrogate; it used to
    # escape as UnicodeEncodeError and kill the AutoSpeaker worker.
    calls = _capture_run(monkeypatch, backend)
    speak("a\ud800b", blocking=True)
    assert len(calls) == 1
    if backend == "windows":
        assert calls[0]["env"]["EVI_TTS_TEXT_0"] == "ab"
    else:
        assert calls[0]["input"] == b"ab\n"


@pytest.mark.parametrize("backend", ["macos", "espeak"])
@pytest.mark.parametrize("bad", ["180; rm -rf ~", "180", 180.0, False, _SneakyInt(180)])
def test_wpm_bad_rate_rejected(monkeypatch, backend, bad):
    calls = _capture_run(monkeypatch, backend)
    with pytest.raises(VoiceError, match="rate"):
        speak("hello", rate=bad, blocking=True)
    assert calls == []


_DEFAULT_RATE_CMD = {"macos": ["say", "-f", "-"], "espeak": ["espeak", "--stdin"]}


@pytest.mark.parametrize("backend", ["macos", "espeak"])
@pytest.mark.parametrize("rate", [0, -5, 1001, 5000])
def test_wpm_out_of_range_rate_speaks_at_default(monkeypatch, backend, rate):
    # Same policy as Windows: an out-of-range int is dropped (default rate),
    # not an error — `evi voice speak x --rate 0` used to speak, and a
    # VoiceError would also stop AutoSpeaker for the rest of the session.
    calls = _capture_run(monkeypatch, backend)
    speak("hello", rate=rate, blocking=True)
    assert len(calls) == 1
    assert calls[0]["cmd"] == _DEFAULT_RATE_CMD[backend]
    assert calls[0]["input"] == b"hello\n"


@pytest.mark.parametrize("rate", [1, 1000])
def test_wpm_rate_bounds_are_inclusive(monkeypatch, rate):
    calls = _capture_run(monkeypatch, "espeak")
    speak("hello", rate=rate, blocking=True)
    assert calls[0]["cmd"] == ["espeak", "-s", str(rate), "--stdin"]


# ---- F5 neural engine (argv-based CLI) ------------------------------------


def test_f5_text_bound_to_its_option(monkeypatch, tmp_path):
    calls = _capture_run(monkeypatch, "none")
    monkeypatch.setattr(voice_mod.shutil, "which", lambda _n: "/usr/bin/f5-tts_infer-cli")
    text = "--output_file=/home/u/.bashrc"
    voice_mod._synth_f5(
        text, tmp_path / "o.wav", model="-m", clone_sample="-ref.wav", language="en",
    )
    cmd = calls[0]["cmd"]
    assert f"--gen_text={text}" in cmd
    assert text not in cmd  # never a standalone argv element
    assert "--model=-m" in cmd and "--ref_audio=-ref.wav" in cmd


def test_f5_refuses_batch_launcher_on_windows(monkeypatch, tmp_path):
    calls = _capture_run(monkeypatch, "none")
    monkeypatch.setattr(
        voice_mod.shutil, "which", lambda _n: r"C:\tools\f5-tts_infer-cli.CMD",
    )
    monkeypatch.setattr(voice_mod.os, "name", "nt")
    with pytest.raises(VoiceError, match="batch"):
        voice_mod._synth_f5(
            "a & calc", tmp_path / "o.wav", model="", clone_sample="", language="en",
        )
    assert calls == []


# ---- the two ways model text reaches TTS ----------------------------------


def test_speak_text_tool_keeps_payload_out_of_script(monkeypatch):
    from evi.tools.voice import speak_text

    calls = _capture_popen(monkeypatch, "windows")
    payload = PAYLOADS[0]
    assert speak_text(payload) == "ok"
    assert _ps_script(calls[0]) == _PS_SCRIPT_ONE_CHUNK
    assert calls[0]["env"]["EVI_TTS_TEXT_0"] == payload


def test_autospeaker_keeps_payload_out_of_script(monkeypatch):
    calls = _capture_run(monkeypatch, "windows")
    done = threading.Event()
    fake_run = voice_mod.subprocess.run

    def signalling_run(cmd, **kwargs):
        result = fake_run(cmd, **kwargs)
        done.set()
        return result

    monkeypatch.setattr(voice_mod.subprocess, "run", signalling_run)
    sp = voice_mod.AutoSpeaker()
    try:
        sp.feed("Sure x’);Start-Process calc;(’ done.")
        assert done.wait(5)
    finally:
        sp.close()
    assert _ps_script(calls[0]) == _PS_SCRIPT_ONE_CHUNK
    spoken = calls[0]["env"]["EVI_TTS_TEXT_0"]
    assert "Start-Process calc" in spoken and "’" in spoken
