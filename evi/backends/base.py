"""Backend ABC + shared data shapes.

A backend is the thing on the other end of `LLMSettings.base_url`. Every
backend must produce an OpenAI-compatible chat client (so the existing
agent loop works unchanged); model management methods are optional and
raise `NotImplementedError` by default.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator, TYPE_CHECKING

if TYPE_CHECKING:
    from openai import OpenAI


@dataclass
class ModelInfo:
    """Backend-agnostic snapshot of one model.

    Most fields are optional — LM Studio's API exposes little besides the
    name; Ollama exposes size, family, parameter count, quantization;
    llama.cpp typically exposes one loaded model only.
    """

    id: str                          # canonical id used by the backend
    backend: str = ""
    name: str = ""                   # display name (defaults to id)
    size_bytes: int | None = None
    family: str | None = None        # e.g. "qwen2", "llama3"
    parameters: str | None = None    # e.g. "7B", "14B"
    quantization: str | None = None  # e.g. "Q4_K_M"
    loaded: bool = False

    def display_name(self) -> str:
        return self.name or self.id


@dataclass
class PullProgress:
    """One update emitted by `Backend.pull_model` during a download."""

    status: str                  # human-readable: "downloading", "verifying", "done"
    downloaded: int | None = None
    total: int | None = None
    detail: str | None = None    # backend-specific extras (digest, layer, …)


class Backend(ABC):
    """Common interface across LM Studio / Ollama / llama.cpp / generic."""

    # Subclasses set these in __init__.
    name: str
    base_url: str
    api_key: str
    request_timeout: float

    # --- required --------------------------------------------------------

    @abstractmethod
    def make_client(self) -> "OpenAI":
        """Return an OpenAI SDK client pointed at this backend."""

    # --- optional --------------------------------------------------------

    def list_models(self) -> list[ModelInfo]:
        """Return models available through this backend.

        Default: best-effort via the OpenAI-standard `/v1/models` endpoint.
        Subclasses override for richer metadata.
        """
        return self._list_via_openai_endpoint()

    def model_info(self, model_id: str) -> ModelInfo | None:
        """Single-model lookup; default falls back to filtering `list_models`."""
        for m in self.list_models():
            if m.id == model_id:
                return m
        return None

    def supports_pull(self) -> bool:
        """Whether `pull_model` is implemented for this backend."""
        return False

    def pull_model(self, model_id: str) -> Iterator[PullProgress]:
        raise NotImplementedError(
            f"{self.name} cannot pull models — download manually and load it "
            "in the backend's UI"
        )

    def delete_model(self, model_id: str) -> None:
        raise NotImplementedError(f"{self.name} has no delete API")

    # --- helpers shared across subclasses -------------------------------

    def _list_via_openai_endpoint(self) -> list[ModelInfo]:
        """Hit `${base_url}/models` (the OpenAI standard) and translate.

        Goes through `portprobe.fast_get`, which fast-fails an unreachable local
        backend instead of stalling on Windows' dual-stack loopback (a closed
        `localhost` port would otherwise block the model picker / settings panel
        for seconds). Never raises — an unreachable backend yields [].
        """
        from evi.portprobe import fast_get

        url = self.base_url.rstrip("/") + "/models"
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else None
        r = fast_get(url, headers=headers)
        if r is None or r.status_code != 200:
            return []
        try:
            data = r.json().get("data", []) or []
        except Exception:
            return []
        return [
            ModelInfo(
                id=str(item.get("id") or item.get("name") or ""),
                backend=self.name,
                loaded=True,
            )
            for item in data
            if (item.get("id") or item.get("name"))
        ]
