"""Model capability detection — one place that answers "what can this model do?"

eVi gates several features on heuristic model-id checks scattered across
modules (vision, reasoning, FIM, audio). This collects them so the UI can show
capability chips and any caller can ask once. All are best-effort substring
heuristics on the model id (the backends don't advertise capabilities), so a
False is "we won't try it" rather than a hard fact.
"""

from __future__ import annotations


def capabilities(model_id: str) -> dict[str, bool]:
    """Return {vision, reasoning, infill, audio, tools, guard, embed} for a model id."""
    from evi.audio_input import model_supports_audio
    from evi.complete import supports_fim
    from evi.embedcap import model_is_embed_class
    from evi.guardmodel import model_is_guard
    from evi.reasoning import model_supports_reasoning
    from evi.toolcalling import model_supports_tools
    from evi.vision import model_supports_vision

    mid = model_id or ""
    return {
        "vision": model_supports_vision(mid),
        "reasoning": model_supports_reasoning(mid),
        "infill": supports_fim(mid),
        "audio": model_supports_audio(mid),
        "tools": model_supports_tools(mid),
        "guard": model_is_guard(mid),
        "embed": model_is_embed_class(mid),
    }


# Short labels for UI chips (emoji + name), in display order.
CHIP_LABELS = {
    "vision": ("👁", "Vision"),
    "reasoning": ("🧠", "Thinking"),
    "infill": ("⌨", "Infill"),
    "audio": ("🎤", "Audio"),
    "tools": ("🔧", "Tools"),
    "guard": ("🛡", "Guard"),
    "embed": ("◆", "Embeddings"),
}
