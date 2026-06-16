"""Curated model registry + recommendation logic.

The list is hand-curated, not pulled from a registry — there's no clean
machine-readable source that scores models on tool-calling reliability
(which is what actually matters for eVi). Bias is toward Qwen2.5 across
the board because its tool calling is best-in-class for local models.

The registry is intentionally short: too many entries paralyses choice.
Add more as you go.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from evi.hardware import HardwareInfo


@dataclass(frozen=True)
class ModelRec:
    """One curated recommendation.

    `min_vram_mb` is the VRAM ceiling for the listed quantization; `min_ram_mb`
    is the equivalent floor for CPU-only inference. The naming uses Ollama
    tags where possible (`qwen2.5:14b`) since Ollama's pull API is the
    smoothest path; LM Studio users can search for the same model under
    its HuggingFace name.
    """

    id: str                  # canonical id (Ollama tag style)
    family: str              # qwen2.5, llama3.1, hermes3, …
    parameters: str          # "7B", "14B", "32B"
    quantization: str        # "Q4_K_M", "Q5_K_M", …
    min_vram_mb: int         # estimated VRAM at this quant
    min_ram_mb: int          # estimated RAM for CPU-only fallback
    tool_calling: str        # "excellent" | "good" | "ok" | "poor"
    role: str                # "chat" | "coder" | "small"
    notes: str = ""
    context_window: int = 0  # native max context (tokens); 0 = backfilled by family
    fast_ok: bool = False    # good as a fast/downshift companion (/fast, ultracode auto-tune)


# The registry. Order matters: we prefer earlier entries within a tier.
REGISTRY: list[ModelRec] = [
    # --- 24+ GB VRAM (P40 / 5090 / dual-GPU) ----------------------------
    ModelRec(
        id="qwen2.5:32b-instruct-q4_K_M",
        family="qwen2.5",
        parameters="32B",
        quantization="Q4_K_M",
        min_vram_mb=22000,
        min_ram_mb=36000,
        tool_calling="excellent",
        role="chat",
        notes="Sweet spot on P40 (24 GB). Best general local model with room for ~16K ctx.",
    ),
    ModelRec(
        id="qwen2.5-coder:32b-instruct-q4_K_M",
        family="qwen2.5-coder",
        parameters="32B",
        quantization="Q4_K_M",
        min_vram_mb=22000,
        min_ram_mb=36000,
        tool_calling="excellent",
        role="coder",
        notes="Best local coding model that still has solid tool calling.",
    ),
    # --- 16 GB VRAM (5070 Ti / 4080) ------------------------------------
    ModelRec(
        id="qwen2.5:14b-instruct-q4_K_M",
        family="qwen2.5",
        parameters="14B",
        quantization="Q4_K_M",
        min_vram_mb=10500,
        min_ram_mb=18000,
        tool_calling="excellent",
        role="chat",
        notes="Big jump in tool-calling reliability vs the 7B. Fits comfortably in 16 GB with 32K ctx.",
    ),
    ModelRec(
        id="qwen2.5-coder:14b-instruct-q4_K_M",
        family="qwen2.5-coder",
        parameters="14B",
        quantization="Q4_K_M",
        min_vram_mb=10500,
        min_ram_mb=18000,
        tool_calling="excellent",
        role="coder",
        notes="Pair with `qwen2.5:14b` for chat; switch via `evi models use`.",
    ),
    # --- 8–12 GB VRAM (3060 Ti / 4060 Ti / 7B sweet spot) ---------------
    ModelRec(
        id="qwen2.5:7b-instruct-q5_K_M",
        family="qwen2.5",
        parameters="7B",
        quantization="Q5_K_M",
        min_vram_mb=6500,
        min_ram_mb=10000,
        tool_calling="excellent",
        role="chat",
        notes="Q5 quant if you have the VRAM — fewer arithmetic errors than Q4.",
    ),
    ModelRec(
        id="qwen2.5:7b-instruct-q4_K_M",
        family="qwen2.5",
        parameters="7B",
        quantization="Q4_K_M",
        min_vram_mb=5500,
        min_ram_mb=8000,
        tool_calling="excellent",
        role="chat",
    ),
    ModelRec(
        id="hermes3:8b-llama3.1-q4_K_M",
        family="hermes3",
        parameters="8B",
        quantization="Q4_K_M",
        min_vram_mb=6000,
        min_ram_mb=9000,
        tool_calling="good",
        role="chat",
        notes="Tool-calling fine-tune on Llama 3.1. Worth trying as a subagent model.",
    ),
    # --- 4–6 GB VRAM (1650 Super / 3050 / older laptops) ----------------
    ModelRec(
        id="qwen2.5:3b-instruct-q4_K_M",
        family="qwen2.5",
        parameters="3B",
        quantization="Q4_K_M",
        min_vram_mb=3000,
        min_ram_mb=5000,
        tool_calling="ok",
        role="chat",
        fast_ok=True,
        notes="Tool calling gets shaky here. Watch for hallucinated tool calls. "
        "Good fast/downshift companion on bigger GPUs.",
    ),
    # --- < 4 GB VRAM / CPU-only / 2 GB laptops --------------------------
    ModelRec(
        id="llama3.2:3b-instruct-q4_K_M",
        family="llama3.2",
        parameters="3B",
        quantization="Q4_K_M",
        min_vram_mb=2500,
        min_ram_mb=4500,
        tool_calling="ok",
        role="chat",
        fast_ok=True,
        notes="Solid small model. Tool calling works most of the time.",
    ),
    ModelRec(
        id="phi3.5:3.8b-mini-instruct-q4_K_M",
        family="phi3",
        parameters="3.8B",
        quantization="Q4_K_M",
        min_vram_mb=3000,
        min_ram_mb=5000,
        tool_calling="ok",
        role="small",
        fast_ok=True,
        notes="Microsoft Phi-3.5-mini. Strong reasoning for its size; long 128K context.",
    ),
    ModelRec(
        id="qwen2.5:1.5b-instruct-q4_K_M",
        family="qwen2.5",
        parameters="1.5B",
        quantization="Q4_K_M",
        min_vram_mb=1500,
        min_ram_mb=2500,
        tool_calling="poor",
        role="small",
        fast_ok=True,
        notes="Fits on 940MX / Pi-class hardware. Don't expect reliable tool calls.",
    ),
    ModelRec(
        id="llama3.2:1b-instruct-q4_K_M",
        family="llama3.2",
        parameters="1B",
        quantization="Q4_K_M",
        min_vram_mb=900,
        min_ram_mb=1500,
        tool_calling="poor",
        role="small",
        fast_ok=True,
        notes="Absolute floor. Acceptable for plain chat only.",
    ),
    ModelRec(
        id="qwen2.5:0.5b-instruct-q4_K_M",
        family="qwen2.5",
        parameters="0.5B",
        quantization="Q4_K_M",
        min_vram_mb=700,
        min_ram_mb=1200,
        tool_calling="poor",
        role="small",
        fast_ok=True,
        notes="Tiniest. Draft/boilerplate only; for speed, not quality.",
    ),
]


# Native context windows per model family (tokens). Used to backfill the
# registry and to answer context_window_for() for ids we don't list verbatim.
_FAMILY_CONTEXT: dict[str, int] = {
    "qwen2.5-coder": 32768,
    "qwen2.5": 32768,
    "qwen3": 32768,
    "hermes3": 131072,
    "llama3.1": 131072,
    "llama3.2": 131072,
    "llama3.3": 131072,
    "mistral": 32768,
    "mixtral": 32768,
    "gemma2": 8192,
    "phi3": 131072,
    "phi4": 16384,
    "deepseek-r1": 65536,
    "command-r": 131072,
}

# Backfill each registry entry's context_window from its family when unset, so
# the registry itself is context-aware.
REGISTRY = [
    m if m.context_window else replace(m, context_window=_FAMILY_CONTEXT.get(m.family, 0))
    for m in REGISTRY
]


def context_window_for(model_id: str) -> int | None:
    """Best-effort native context window (tokens) for a model id, or None.

    Tries an exact registry id, then a family-prefix match (so unlisted tags
    like ``qwen2.5:14b-instruct-q8_0`` still resolve). Longer family keys win.
    """
    mid = (model_id or "").strip().lower()
    if not mid:
        return None
    for m in REGISTRY:
        if m.id.lower() == mid and m.context_window:
            return m.context_window
    for fam in sorted(_FAMILY_CONTEXT, key=len, reverse=True):
        if mid.startswith(fam) or f"/{fam}" in mid:
            return _FAMILY_CONTEXT[fam]
    return None


def _pick_fast(
    *, vram_mb: int | None = None, ram_mb: int | None = None
) -> ModelRec | None:
    """The best small/fast companion that fits — for `/fast` swaps and ultracode
    downshift. Largest `fast_ok` model within budget (registry is biggest-first).
    A fast model SWAPS in (not co-resident), so it only needs to fit alone."""
    for m in (m for m in REGISTRY if m.fast_ok):
        if vram_mb is not None and m.min_vram_mb <= vram_mb:
            return m
        if ram_mb is not None and m.min_ram_mb <= ram_mb:
            return m
    return None


@dataclass
class Recommendation:
    mode: str             # "gpu" | "cpu" | "remote-only"
    chat: ModelRec | None
    coder: ModelRec | None
    notes: list[str]
    fast: ModelRec | None = None   # small/downshift companion (/fast, ultracode)


def recommend(hw: HardwareInfo) -> Recommendation:
    """Pick the best chat + coder model for the detected hardware.

    Returns `mode="remote-only"` when neither GPU nor RAM is sufficient for
    anything usable — caller should suggest pointing at a remote backend.
    """
    notes: list[str] = []
    primary = hw.primary_gpu

    if primary is None:
        notes.append("No NVIDIA GPU detected — falling back to CPU.")
        ram_mb = hw.ram_total_bytes // (1024 * 1024)
        chat = _pick(REGISTRY, role="chat", ram_mb=ram_mb)
        coder = _pick(REGISTRY, role="coder", ram_mb=ram_mb)
        fast = _pick_fast(ram_mb=ram_mb)
        if chat is None:
            return Recommendation(mode="remote-only", chat=None, coder=None, notes=notes + [
                "Even CPU inference would be slow with the available RAM.",
                "Point eVi at a remote backend (your AI server) instead.",
            ])
        return Recommendation(mode="cpu", chat=chat, coder=coder, notes=notes, fast=fast)

    vram = primary.vram_total_mb
    notes.append(f"Primary GPU: {primary.name} ({vram} MB VRAM).")
    if vram < 2500:
        notes.append(
            "GPU VRAM is below the threshold where offload helps — system "
            "RAM bandwidth will outperform PCIe-shuttled GPU inference. "
            "Treat this as CPU-only."
        )
        # Fall through to CPU rec.
        ram_mb = hw.ram_total_bytes // (1024 * 1024)
        chat = _pick(REGISTRY, role="chat", ram_mb=ram_mb)
        coder = _pick(REGISTRY, role="coder", ram_mb=ram_mb)
        fast = _pick_fast(ram_mb=ram_mb)
        if chat is None:
            return Recommendation(mode="remote-only", chat=None, coder=None, notes=notes)
        return Recommendation(mode="cpu", chat=chat, coder=coder, notes=notes, fast=fast)

    if primary.compute_capability and float(primary.compute_capability) < 6.0:
        notes.append(
            "GPU compute capability is pre-Pascal — modern features (FP16, "
            "flash attention) won't work. Stick to GGUF Q4/Q5 quants."
        )

    chat = _pick(REGISTRY, role="chat", vram_mb=vram)
    coder = _pick(REGISTRY, role="coder", vram_mb=vram)
    fast = _pick_fast(vram_mb=vram)
    return Recommendation(mode="gpu", chat=chat, coder=coder, notes=notes, fast=fast)


def _pick(
    registry: list[ModelRec],
    *,
    role: str,
    vram_mb: int | None = None,
    ram_mb: int | None = None,
) -> ModelRec | None:
    """Return the largest model from `registry` that fits within budget."""
    candidates = [m for m in registry if m.role == role]
    for m in candidates:  # registry is roughly biggest-first
        if vram_mb is not None and m.min_vram_mb <= vram_mb:
            return m
        if ram_mb is not None and m.min_ram_mb <= ram_mb:
            return m
    return None


# First-run default is capped at this many billion params so the very first
# download is small (~2 GB) and the first reply is quick — even on a big GPU,
# where a 14B/32B is *better* but a 20 GB pull is a terrible first impression.
# `recommend()` still surfaces the bigger hardware-optimal model as an upgrade.
_FIRST_RUN_MAX_PARAMS_B = 3.0


def _params_b(parameters: str) -> float:
    """Parse a `ModelRec.parameters` string like '7B' / '1.5B' to a float."""
    try:
        return float(parameters.strip().upper().rstrip("B"))
    except ValueError:
        return 0.0


def first_run_model(hw: HardwareInfo) -> str:
    """Pick the model to auto-pull on first run: the largest chat/small model
    at or under `_FIRST_RUN_MAX_PARAMS_B` that fits the detected hardware.

    Returns an Ollama-style tag (e.g. ``qwen2.5:3b-instruct-q4_K_M``). Always
    returns *something* — the smallest known model as a last resort — so a fresh
    user can always get to a first chat.
    """
    small_first = [
        m for m in REGISTRY
        if m.role in ("chat", "small") and _params_b(m.parameters) <= _FIRST_RUN_MAX_PARAMS_B
    ]
    # REGISTRY is biggest-first, so the first entry that fits is the best pick.
    vram_mb = hw.primary_gpu.vram_total_mb if hw.primary_gpu else None
    ram_mb = hw.ram_total_bytes // (1024 * 1024)
    for m in small_first:
        if vram_mb is not None and vram_mb >= 2500 and m.min_vram_mb <= vram_mb:
            return m.id
        if m.min_ram_mb <= ram_mb:
            return m.id
    return small_first[-1].id  # smallest known model, last resort
