"""Voice — cross-platform TTS + STT.

TTS uses what's already on the box, no Python deps:
- Windows: PowerShell `System.Speech.Synthesis.SpeechSynthesizer`
- macOS:   `say`
- Linux:   `espeak-ng` (if installed), else `espeak`, else error

STT uses `faster-whisper` (CTranslate2-backed whisper) + `sounddevice` for
mic capture. Both are optional — install `evi[stt]` to enable. The model
is downloaded on first use to the HF cache (~75 MB for the `tiny.en`
default; larger models give better accuracy at the cost of latency).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


class VoiceError(RuntimeError):
    """Raised when a voice backend is missing or a spawn / capture fails."""


def detect_backend() -> str:
    """Return one of {"windows", "macos", "espeak-ng", "espeak", "none"}."""
    if os.name == "nt":
        return "windows"
    if hasattr(os, "uname") and os.uname().sysname == "Darwin":
        return "macos"
    if shutil.which("espeak-ng"):
        return "espeak-ng"
    if shutil.which("espeak"):
        return "espeak"
    return "none"


def speak(text: str, *, rate: int | None = None, blocking: bool = True) -> None:
    """Speak `text` aloud via the platform TTS engine.

    `rate` is backend-specific (words per minute on Windows, -r on espeak).
    `blocking=False` returns immediately while the speech plays in the
    background — useful for long messages.
    """
    text = text.strip()
    if not text:
        return
    backend = detect_backend()
    if backend == "none":
        raise VoiceError(
            "no TTS backend found — install espeak-ng (Linux) "
            "or use a Mac/Windows host"
        )

    cmd: list[str]
    if backend == "windows":
        # PowerShell one-liner. We escape single quotes by doubling them.
        escaped = text.replace("'", "''")
        rate_part = f"$s.Rate = {rate};" if rate is not None else ""
        ps = (
            "Add-Type -AssemblyName System.Speech;"
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            f"{rate_part}"
            f"$s.Speak('{escaped}')"
        )
        cmd = ["powershell.exe", "-NoProfile", "-Command", ps]
    elif backend == "macos":
        cmd = ["say"]
        if rate is not None:
            cmd += ["-r", str(rate)]
        cmd += [text]
    else:  # espeak / espeak-ng
        cmd = [backend]
        if rate is not None:
            cmd += ["-s", str(rate)]
        cmd += [text]

    if blocking:
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise VoiceError(f"TTS failed: {exc}") from exc
    else:
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise VoiceError(f"TTS spawn failed: {exc}") from exc


# ---- STT -----------------------------------------------------------------


# Cache the loaded whisper model across calls — first load takes 1-3 s and
# downloads the model on miss; we don't want to pay that per `listen()`.
_WHISPER_MODEL: object | None = None
_WHISPER_MODEL_KEY: tuple[str, str, str] | None = None


def _load_whisper(model_name: str, device: str, compute_type: str):
    """Return a cached `faster_whisper.WhisperModel`. Loads on first call."""
    global _WHISPER_MODEL, _WHISPER_MODEL_KEY
    key = (model_name, device, compute_type)
    if _WHISPER_MODEL is not None and _WHISPER_MODEL_KEY == key:
        return _WHISPER_MODEL
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise VoiceError(
            "STT requires faster-whisper + sounddevice — "
            "install with: pip install 'evi-assistant[stt]'"
        ) from exc
    _WHISPER_MODEL = WhisperModel(model_name, device=device, compute_type=compute_type)
    _WHISPER_MODEL_KEY = key
    return _WHISPER_MODEL


def listen(
    *,
    duration: float = 5.0,
    sample_rate: int = 16000,
    model: str = "tiny.en",
    device: str = "cpu",
    compute_type: str = "int8",
    language: str | None = None,
) -> str:
    """Record `duration` seconds from the default mic and transcribe.

    Args:
        duration: seconds to record. Use `listen_until_silence()` for VAD.
        sample_rate: 16 kHz is what whisper trained on; don't change unless
            you know why.
        model: faster-whisper model id. Order of speed → accuracy:
            tiny.en, base.en, small.en, medium.en, large-v3.
            English-only `.en` variants are smaller + faster for English.
        device: "cpu" or "cuda".
        compute_type: "int8" (CPU default), "int8_float16" (CUDA cheap),
            "float16" (CUDA full precision).
        language: ISO 639-1 hint ("en", "es", …) or None to auto-detect.
    """
    try:
        import sounddevice as sd  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]
    except ImportError as exc:
        raise VoiceError(
            "STT requires sounddevice + numpy — "
            "install with: pip install 'evi-assistant[stt]'"
        ) from exc

    audio = sd.rec(
        int(duration * sample_rate),
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
    )
    sd.wait()
    samples = audio.reshape(-1).astype(np.float32)
    return _transcribe(samples, sample_rate, model, device, compute_type, language)


def transcribe_wav(
    path: Path | str,
    *,
    model: str = "tiny.en",
    device: str = "cpu",
    compute_type: str = "int8",
    language: str | None = None,
) -> str:
    """Transcribe an existing audio file (any format faster-whisper accepts)."""
    whisper = _load_whisper(model, device, compute_type)
    segments, _ = whisper.transcribe(str(path), language=language, beam_size=1)
    return " ".join(seg.text.strip() for seg in segments).strip()


def _transcribe(
    samples,
    sample_rate: int,
    model: str,
    device: str,
    compute_type: str,
    language: str | None,
) -> str:
    """Run whisper on an in-memory float32 mono buffer."""
    whisper = _load_whisper(model, device, compute_type)
    segments, _ = whisper.transcribe(
        samples, language=language, beam_size=1, vad_filter=True
    )
    return " ".join(seg.text.strip() for seg in segments).strip()


# ---- AutoSpeaker — streaming sentence-by-sentence TTS -------------------


import queue as _queue  # noqa: E402  (kept here to avoid clutter at top)
import re as _re  # noqa: E402
import threading as _threading  # noqa: E402


# A sentence ends on `.`, `!`, `?` followed by whitespace OR end-of-input.
# Allows `e.g.` and `Mr.` to occasionally slip through; that's fine — the
# next chunk will pick them up at the next real boundary.
_SENT_END_RE = _re.compile(r"([.!?]+)(\s+|$)")

# We skip code fences entirely — speaking code aloud is noise.
_CODE_FENCE_RE = _re.compile(r"```.*?```", _re.S)
# Same for inline code spans and obvious URL runs.
_INLINE_CODE_RE = _re.compile(r"`[^`]+`")
_URL_RE = _re.compile(r"https?://\S+")


def _clean_for_tts(text: str) -> str:
    """Strip code, URLs, and excess punctuation before sending to TTS."""
    text = _CODE_FENCE_RE.sub(" [code block] ", text)
    text = _INLINE_CODE_RE.sub(" code ", text)
    text = _URL_RE.sub(" link ", text)
    # Collapse runs of newlines into a single sentence break.
    text = _re.sub(r"\n+", ". ", text)
    text = _re.sub(r"\s+", " ", text).strip()
    return text


class AutoSpeaker:
    """Buffer streaming text and speak completed sentences.

    Pattern of use (CLI):

        speaker = AutoSpeaker()
        try:
            for event in agent.chat(...):
                if isinstance(event, TextDelta):
                    speaker.feed(event.text)
                elif isinstance(event, Done):
                    speaker.flush()
        finally:
            speaker.close()

    Speech runs on a background thread that pulls from a queue, so the
    main loop stays responsive even on slow TTS engines (espeak in
    particular can stutter on long passages).
    """

    def __init__(self, *, rate: int | None = None) -> None:
        self.rate = rate
        self._buf = ""
        self._q: _queue.Queue[str | None] = _queue.Queue()
        self._stopped = False
        self._thread = _threading.Thread(
            target=self._worker, name="evi-autospeaker", daemon=True,
        )
        self._thread.start()

    def feed(self, delta: str) -> None:
        """Accept a chunk of streamed text. Emits completed sentences for
        speech and holds any trailing partial."""
        if self._stopped or not delta:
            return
        self._buf += delta
        out: list[str] = []
        last_end = 0
        for m in _SENT_END_RE.finditer(self._buf):
            chunk = self._buf[last_end : m.end()]
            cleaned = _clean_for_tts(chunk)
            if cleaned:
                out.append(cleaned)
            last_end = m.end()
        if last_end:
            self._buf = self._buf[last_end:]
        for line in out:
            self._q.put(line)

    def flush(self) -> None:
        """Speak whatever's still buffered (no terminator seen)."""
        if self._stopped:
            return
        if self._buf.strip():
            cleaned = _clean_for_tts(self._buf)
            if cleaned:
                self._q.put(cleaned)
            self._buf = ""

    def close(self) -> None:
        """Stop the worker. Pending sentences are dropped."""
        if self._stopped:
            return
        self._stopped = True
        # Sentinel — worker exits the loop.
        self._q.put(None)
        # Don't join; daemon thread. Caller usually moves on immediately.

    def _worker(self) -> None:
        while True:
            item = self._q.get()
            if item is None or self._stopped:
                return
            try:
                # Blocking speak so chunks don't overlap audibly.
                speak(item, rate=self.rate, blocking=True)
            except VoiceError:
                # Backend missing or platform issue — stop trying.
                self._stopped = True
                return


# ---- AutoListener — continuous VAD-driven listening --------------------


from typing import Callable as _Callable  # noqa: E402


class AutoListener:
    """Always-on mic listener with energy-based voice activity detection.

    Architecture:
    - Background thread opens a sounddevice InputStream.
    - Frames (30 ms at 16 kHz mono) flow through a state machine:
        idle → speaking (after K loud frames)
              → idle (after M silent frames)
    - On end-of-utterance, accumulated audio is transcribed via Whisper
      and handed to `callback(text)`.

    Energy-based VAD is a deliberate choice — no native deps, works in a
    quiet room, and the wake-phrase gate compensates for false positives
    (anyone who isn't talking *to* Evi gets ignored). For noisy
    environments, swap in webrtcvad or silero-vad behind the same shape.

    Lifecycle:
        listener = AutoListener(on_utterance=callback)
        listener.start()
        ...
        listener.stop()
    """

    def __init__(
        self,
        on_utterance: _Callable[[str], None],
        *,
        sample_rate: int = 16000,
        frame_ms: int = 30,
        rms_threshold: float = 0.015,
        speech_start_frames: int = 6,   # ~180 ms of voice to start a clip
        speech_end_frames: int = 25,    # ~750 ms of silence ends it
        max_clip_seconds: float = 30.0, # cap each utterance length
        wake_phrase: str | None = None,
        model: str = "tiny.en",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str | None = None,
        debug: bool = False,
    ) -> None:
        self.on_utterance = on_utterance
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.frame_size = int(sample_rate * frame_ms / 1000)
        self.rms_threshold = rms_threshold
        self.speech_start_frames = speech_start_frames
        self.speech_end_frames = speech_end_frames
        self.max_frames = int(max_clip_seconds * 1000 / frame_ms)
        # Normalise the wake phrase to a lowercase substring matcher.
        self.wake_phrase = (wake_phrase or "").strip().lower() or None
        self.model = model
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.debug = debug

        self._stream = None
        self._stop_evt = _threading.Event()
        self._pause_evt = _threading.Event()  # set => listener ignores audio
        self._thread: _threading.Thread | None = None

    # --- public API ----------------------------------------------------

    def start(self) -> None:
        """Begin listening. Blocks briefly while opening the audio device."""
        if self._thread is not None and self._thread.is_alive():
            return
        try:
            import numpy as np  # noqa: F401
            import sounddevice  # noqa: F401
        except ImportError as exc:
            raise VoiceError(
                "voice loop requires sounddevice + numpy — "
                "install with: pip install 'evi-assistant[stt]'"
            ) from exc

        self._stop_evt.clear()
        self._pause_evt.clear()
        self._thread = _threading.Thread(
            target=self._run, name="evi-voice-loop", daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Tell the listener to wind down. Returns once the thread joins."""
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def pause(self) -> None:
        """Temporarily stop capturing utterances.

        Useful while Evi is speaking — without a pause the listener would
        transcribe its own TTS output and re-fire. Frames still flow into
        the underlying sounddevice queue; they're discarded on resume.
        """
        self._pause_evt.set()

    def resume(self) -> None:
        """Re-enable capture after a pause(). Drops any backlogged audio."""
        self._pause_evt.clear()

    # --- internals -----------------------------------------------------

    def _rms(self, frame) -> float:
        """Root-mean-square amplitude of a float32 mono frame."""
        import numpy as np
        # frame is shaped (frame_size, 1) for mono.
        x = frame.reshape(-1)
        return float(np.sqrt(np.mean(x * x))) if x.size else 0.0

    def _run(self) -> None:
        import sounddevice as sd
        import numpy as np

        # Pre-load whisper so the first utterance doesn't eat the cold-start.
        try:
            _load_whisper(self.model, self.device, self.compute_type)
        except VoiceError as exc:
            if self.debug:
                print(f"[autolisten] whisper load failed: {exc}")
            return

        clip: list = []          # accumulated frames for the current utterance
        in_speech = False
        loud_streak = 0
        silent_streak = 0

        # Sounddevice InputStream pushes us frames via a callback running on
        # its own thread; we synchronise via a queue.
        frame_q: _queue.Queue = _queue.Queue()

        def _on_audio(indata, frames, time_info, status) -> None:
            # Copy because indata's buffer is reused by sounddevice.
            frame_q.put(indata.copy())

        def _drain() -> None:
            """Empty the frame queue (used after pauses + after utterances)."""
            try:
                while True:
                    frame_q.get_nowait()
            except _queue.Empty:
                return

        try:
            with sd.InputStream(
                samplerate=self.sample_rate,
                blocksize=self.frame_size,
                channels=1,
                dtype="float32",
                callback=_on_audio,
            ):
                while not self._stop_evt.is_set():
                    # Paused — discard anything that arrived and reset state
                    # so we don't trail a half-spoken clip when we resume.
                    if self._pause_evt.is_set():
                        _drain()
                        clip = []
                        in_speech = False
                        loud_streak = 0
                        silent_streak = 0
                        if self._stop_evt.wait(timeout=0.05):
                            break
                        continue

                    try:
                        frame = frame_q.get(timeout=0.1)
                    except _queue.Empty:
                        continue

                    rms = self._rms(frame)
                    is_voice = rms > self.rms_threshold

                    if not in_speech:
                        if is_voice:
                            loud_streak += 1
                            clip.append(frame)
                            if loud_streak >= self.speech_start_frames:
                                in_speech = True
                                silent_streak = 0
                                if self.debug:
                                    print(f"[autolisten] start (rms={rms:.4f})")
                        else:
                            loud_streak = 0
                            # Keep a small ringback so the start of an utterance
                            # isn't cut off — last few quiet frames.
                            clip.append(frame)
                            if len(clip) > self.speech_start_frames:
                                clip.pop(0)
                    else:
                        clip.append(frame)
                        if is_voice:
                            silent_streak = 0
                        else:
                            silent_streak += 1
                        # End of utterance OR hit the max-clip cap.
                        if (
                            silent_streak >= self.speech_end_frames
                            or len(clip) >= self.max_frames
                        ):
                            samples = np.concatenate([f.reshape(-1) for f in clip])
                            clip = []
                            in_speech = False
                            loud_streak = 0
                            silent_streak = 0
                            self._handle_utterance(samples)
                            # The callback may have spoken — drop any audio
                            # that arrived during handling so we don't
                            # transcribe our own voice on the next pass.
                            _drain()
        except Exception as exc:  # noqa: BLE001
            if self.debug:
                print(f"[autolisten] audio stream error: {exc}")

    def _handle_utterance(self, samples) -> None:
        """Transcribe a recorded clip and gate on wake phrase if configured."""
        try:
            text = _transcribe(
                samples,
                self.sample_rate,
                self.model,
                self.device,
                self.compute_type,
                self.language,
            )
        except Exception as exc:  # noqa: BLE001
            if self.debug:
                print(f"[autolisten] transcribe failed: {exc}")
            return

        if not text:
            return
        if self.debug:
            print(f"[autolisten] heard: {text!r}")

        if self.wake_phrase:
            lower = text.lower()
            idx = lower.find(self.wake_phrase)
            if idx < 0:
                return
            # Strip the wake phrase (and surrounding punctuation) so the
            # callback sees only the actual request.
            after = text[idx + len(self.wake_phrase) :]
            text = after.lstrip(" ,.:;!?").strip()
            if not text:
                return

        try:
            self.on_utterance(text)
        except Exception as exc:  # noqa: BLE001
            if self.debug:
                print(f"[autolisten] callback raised: {exc}")
