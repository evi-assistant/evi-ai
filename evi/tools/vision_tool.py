"""Vision tool — describe/answer questions about an image with a small VLM.

Lets the agent caption or visually-inspect an image WITHOUT loading the main
instruct/coder model: it routes to the configured ``[models] vision`` specialty
(e.g. ``moondream`` or ``qwen2.5vl:3b``). When no vision specialty is set it
falls back to the main chat model IF that model is itself a VLM; otherwise it
returns a clear hint to configure one. Distinct from ``image_comfy`` (which
GENERATES images) — this READS them.
"""

from __future__ import annotations

from pathlib import Path

from evi.tools.base import tool

_MAX_OUTPUT_BYTES = 16 * 1024


@tool(
    description=(
        "Describe or answer a question about an image using a vision model. "
        "Routes to the configured vision specialty model ([models] vision, "
        "e.g. moondream / qwen2.5vl) so it doesn't load the main model; falls "
        "back to the main chat model only if it is itself a VLM. `prompt` is "
        "what to ask about the image (default: describe it)."
    ),
    category="vision",
)
def describe_image(path: str, prompt: str = "Describe this image in detail.") -> str:
    target = Path(path).expanduser()
    if not target.is_file():
        return f"ERROR: no such file: {target}"

    from evi.llm.specialty import load_registry

    reg = load_registry()

    # 1) Dedicated vision specialty model, if configured.
    if reg.model_id("vision"):
        try:
            out = reg.run_image("vision", target, prompt, max_tokens=_MAX_OUTPUT_BYTES)
        except Exception as exc:  # noqa: BLE001
            return f"ERROR: vision model ({reg.model_id('vision')}) failed: {exc}"
        return _clip(out)

    # 2) Fall back to the main chat model only if it can see.
    from evi.vision import model_supports_vision

    main = reg.llm.model
    if model_supports_vision(main):
        try:
            from evi.llm.client import make_client
            from evi.vision import build_image_content

            client = make_client(reg.llm)
            resp = client.chat.completions.create(
                model=main,
                messages=[{"role": "user", "content": build_image_content(prompt, [target])}],
                temperature=0.0,
                max_tokens=_MAX_OUTPUT_BYTES,
            )
            out = (resp.choices[0].message.content or "") if resp.choices else ""
        except Exception as exc:  # noqa: BLE001
            return f"ERROR: vision via main model ({main}) failed: {exc}"
        return _clip(out)

    return (
        "ERROR: no vision model available — set [models] vision (e.g. "
        "`evi models specialty set vision moondream`) or use a VLM as the main model."
    )


def _clip(text: str) -> str:
    text = (text or "").strip()
    if len(text) > _MAX_OUTPUT_BYTES:
        text = text[:_MAX_OUTPUT_BYTES] + "\n…(truncated)"
    return text or "(no description)"
