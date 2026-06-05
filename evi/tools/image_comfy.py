"""ComfyUI image-generation tool.

Submits a text2img workflow to a local ComfyUI server, polls /history for
completion, fetches the rendered PNGs via /view, and saves them under
%USERPROFILE%/.evi/images/. The tool returns the saved file paths so the CLI
can echo them and the (future) web UI can embed them.

Defaults are pulled from `ComfySettings` in config.toml; per-call args override
them. The workflow template assumes a stock ComfyUI install with a SDXL-style
checkpoint — swap `default_checkpoint` in config for SD1.5 models.
"""

from __future__ import annotations

import json
import random
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from evi.config import IMAGE_DIR, Config, ensure_dirs
from evi.tools.base import tool


_POLL_INTERVAL_SECONDS = 1.0
_POLL_TIMEOUT_SECONDS = 300  # 5 min ceiling for a single generation
_HTTP_TIMEOUT_SECONDS = 30.0


def _default_workflow(
    *,
    prompt: str,
    negative_prompt: str,
    checkpoint: str,
    width: int,
    height: int,
    steps: int,
    cfg: float,
    seed: int,
    sampler: str,
    scheduler: str,
) -> dict[str, Any]:
    """Build a stock SDXL-style text2img workflow graph for ComfyUI.

    Node ids match the workflow ComfyUI's "default" template uses, so this
    works against any ComfyUI install with a compatible checkpoint loaded.
    """
    return {
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": sampler,
                "scheduler": scheduler,
                "denoise": 1.0,
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
            },
        },
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": checkpoint},
        },
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["4", 1]},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative_prompt, "clip": ["4", 1]},
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "evi", "images": ["8", 0]},
        },
    }


def _submit(
    client: httpx.Client, base_url: str, workflow: dict[str, Any], client_id: str
) -> str:
    """POST the workflow to /prompt, return the prompt_id ComfyUI assigns."""
    r = client.post(
        f"{base_url}/prompt",
        json={"prompt": workflow, "client_id": client_id},
    )
    r.raise_for_status()
    data = r.json()
    pid = data.get("prompt_id")
    if not pid:
        raise RuntimeError(f"ComfyUI did not return a prompt_id: {data}")
    return pid


def _poll_history(
    client: httpx.Client, base_url: str, prompt_id: str
) -> dict[str, Any]:
    """Block until /history/{prompt_id} has outputs, return the entry."""
    deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        r = client.get(f"{base_url}/history/{prompt_id}")
        r.raise_for_status()
        data = r.json()
        entry = data.get(prompt_id)
        if entry and entry.get("outputs"):
            return entry
        time.sleep(_POLL_INTERVAL_SECONDS)
    raise TimeoutError(
        f"ComfyUI generation {prompt_id} did not complete within "
        f"{_POLL_TIMEOUT_SECONDS}s"
    )


def _collect_image_refs(history_entry: dict[str, Any]) -> list[dict[str, str]]:
    """Pull {filename, subfolder, type} dicts out of any node's `images` list."""
    refs: list[dict[str, str]] = []
    for node_output in history_entry.get("outputs", {}).values():
        for img in node_output.get("images", []) or []:
            refs.append(
                {
                    "filename": img.get("filename", ""),
                    "subfolder": img.get("subfolder", ""),
                    "type": img.get("type", "output"),
                }
            )
    return refs


def _download(
    client: httpx.Client, base_url: str, ref: dict[str, str], dest: Path
) -> None:
    query = urlencode(ref)
    r = client.get(f"{base_url}/view?{query}")
    r.raise_for_status()
    dest.write_bytes(r.content)


@tool(
    description=(
        "Generate an image from a text prompt using the local ComfyUI server. "
        "Returns one or more saved file paths (one per line). Use this when "
        "the user asks for a picture, illustration, or artwork."
    ),
    category="image",
)
def generate_image(
    prompt: str,
    negative_prompt: str = "",
    width: int = 0,
    height: int = 0,
    steps: int = 0,
    seed: int = -1,
    cfg: float = 7.0,
    sampler: str = "euler",
    scheduler: str = "normal",
) -> str:
    config = Config.load()
    comfy = config.comfy
    ensure_dirs()

    w = width or comfy.default_width
    h = height or comfy.default_height
    s = steps or comfy.default_steps
    actual_seed = random.randint(0, 2**31 - 1) if seed < 0 else seed

    workflow = _default_workflow(
        prompt=prompt,
        negative_prompt=negative_prompt,
        checkpoint=comfy.default_checkpoint,
        width=w,
        height=h,
        steps=s,
        cfg=cfg,
        seed=actual_seed,
        sampler=sampler,
        scheduler=scheduler,
    )
    client_id = uuid.uuid4().hex

    with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        prompt_id = _submit(client, comfy.base_url, workflow, client_id)
        entry = _poll_history(client, comfy.base_url, prompt_id)
        refs = _collect_image_refs(entry)
        if not refs:
            return f"ERROR: ComfyUI returned no images for prompt_id={prompt_id}"

        saved: list[Path] = []
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        for i, ref in enumerate(refs):
            ext = Path(ref["filename"]).suffix or ".png"
            dest = IMAGE_DIR / f"{stamp}_{actual_seed}_{i}{ext}"
            _download(client, comfy.base_url, ref, dest)
            saved.append(dest)

    summary = {
        "prompt_id": prompt_id,
        "seed": actual_seed,
        "paths": [str(p) for p in saved],
    }
    return json.dumps(summary)
