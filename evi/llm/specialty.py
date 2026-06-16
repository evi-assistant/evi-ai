"""Specialty-model registry — route a single task to a small dedicated model.

eVi normally runs one chat model (plus an optional `fast_model` and keyword
routing that swap the WHOLE model for a turn). Specialty models are different:
a tool can call a small task-specific model (an OCR/vision VLM today) WITHOUT
unloading or swapping the main instruct/coder model. Configured under
``[models]`` (see :class:`evi.config.SpecialtyModels`).

Each chat-VLM specialty (``ocr``, ``vision``) is served over the same OpenAI
image schema eVi already uses, on the ``[llm]`` backend by default or a
per-specialty ``*_base_url`` / ``*_backend`` override (e.g. a separate vLLM
server). Clients are built lazily and cached, so co-resident specialties cost
nothing until first use and never tear down the primary model's client.

STT/TTS specialties (``stt``, ``tts``) are plain id strings consumed by the
voice subsystem (``evi/voice.py``), not chat clients — they're exposed here via
:meth:`SpecialtyRegistry.model_id` for a single source of truth.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # avoid import cost / cycles at module load
    from evi.config import LLMSettings, SpecialtyModels

# Tasks whose specialty model is a chat-VLM reachable over the OpenAI schema.
_VLM_TASKS = ("ocr", "vision")


class SpecialtyRegistry:
    """Lazily build + cache OpenAI clients for the configured specialty models."""

    def __init__(self, llm: "LLMSettings", specialty: "SpecialtyModels") -> None:
        self.llm = llm
        self.spec = specialty
        self._clients: dict[str, Any] = {}

    def model_id(self, task: str) -> str:
        """Configured model id for a task ("" if unset)."""
        return (getattr(self.spec, task, "") or "").strip()

    def client_for(self, task: str):
        """An OpenAI client pointed at the specialty model for `task`, or None
        when the task is unconfigured. Cached after first build."""
        mid = self.model_id(task)
        if not mid:
            return None
        if task in self._clients:
            return self._clients[task]
        sub = replace(self.llm, model=mid)
        base_url = (getattr(self.spec, f"{task}_base_url", "") or "").strip()
        backend = (getattr(self.spec, f"{task}_backend", "") or "").strip()
        if base_url:
            sub = replace(sub, base_url=base_url)
        if backend:
            sub = replace(sub, backend=backend)
        from evi.llm.client import make_client

        client = make_client(sub)
        self._clients[task] = client
        return client

    def run_image(
        self, task: str, image_path: str | Path, prompt: str, *, max_tokens: int = 4096
    ) -> str | None:
        """Send `image_path` + `prompt` to the `task` specialty VLM and return
        its text, or None if the task is unconfigured. Raises on a backend error
        so callers can fall back (e.g. to tesseract)."""
        client = self.client_for(task)
        if client is None:
            return None
        from evi.vision import build_image_content

        content = build_image_content(prompt, [image_path])
        resp = client.chat.completions.create(
            model=self.model_id(task),
            messages=[{"role": "user", "content": content}],
            temperature=0.0,
            max_tokens=max_tokens,
        )
        return (resp.choices[0].message.content or "") if resp.choices else ""


def load_registry() -> SpecialtyRegistry:
    """Build a registry from the active on-disk config. Cheap; safe to call per
    tool invocation so the current [models] config (incl. project overlay) wins."""
    from evi.config import Config

    cfg = Config.load()
    return SpecialtyRegistry(cfg.llm, cfg.models)
