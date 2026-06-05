"""Tests for the ComfyUI image-gen tool.

The HTTP boundary is mocked via httpx.MockTransport so the tool runs end-to-end
without a real ComfyUI server. IMAGE_DIR is redirected into tmp_path so the
generated files do not pollute the user's ~/.evi/images.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import evi.tools.image_comfy as image_comfy
from evi.tools.base import REGISTRY


@pytest.fixture
def patched_comfy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Redirect IMAGE_DIR and stub httpx.Client to a deterministic transport."""

    monkeypatch.setattr(image_comfy, "IMAGE_DIR", tmp_path)
    # Skip the real-time poll delay.
    monkeypatch.setattr(image_comfy, "_POLL_INTERVAL_SECONDS", 0)

    calls: dict[str, int] = {"prompt": 0, "history": 0, "view": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/prompt":
            calls["prompt"] += 1
            return httpx.Response(200, json={"prompt_id": "pid-xyz"})
        if path == "/history/pid-xyz":
            calls["history"] += 1
            # First poll returns empty, second returns a result — exercises poll loop.
            if calls["history"] < 2:
                return httpx.Response(200, json={})
            return httpx.Response(
                200,
                json={
                    "pid-xyz": {
                        "outputs": {
                            "9": {
                                "images": [
                                    {
                                        "filename": "evi_00001_.png",
                                        "subfolder": "",
                                        "type": "output",
                                    }
                                ]
                            }
                        }
                    }
                },
            )
        if path == "/view":
            calls["view"] += 1
            return httpx.Response(200, content=b"\x89PNG\r\n\x1a\nFAKE")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=transport)

    monkeypatch.setattr(image_comfy.httpx, "Client", fake_client)
    return calls, tmp_path


def test_generate_image_happy_path(patched_comfy) -> None:
    calls, tmp_path = patched_comfy
    out = REGISTRY["generate_image"].call(
        json.dumps({"prompt": "a cyberpunk cat", "seed": 42})
    )
    data = json.loads(out)
    assert data["prompt_id"] == "pid-xyz"
    assert data["seed"] == 42
    assert len(data["paths"]) == 1
    saved = Path(data["paths"][0])
    assert saved.parent == tmp_path
    assert saved.exists()
    assert saved.read_bytes().startswith(b"\x89PNG")
    assert calls["prompt"] == 1
    assert calls["history"] >= 2  # polled at least twice (empty, then ready)
    assert calls["view"] == 1


def test_workflow_uses_overridden_params() -> None:
    wf = image_comfy._default_workflow(
        prompt="hello",
        negative_prompt="ugly",
        checkpoint="custom.safetensors",
        width=512,
        height=768,
        steps=12,
        cfg=6.5,
        seed=7,
        sampler="dpmpp_2m",
        scheduler="karras",
    )
    assert wf["4"]["inputs"]["ckpt_name"] == "custom.safetensors"
    assert wf["5"]["inputs"]["width"] == 512
    assert wf["5"]["inputs"]["height"] == 768
    assert wf["3"]["inputs"]["steps"] == 12
    assert wf["3"]["inputs"]["cfg"] == 6.5
    assert wf["3"]["inputs"]["seed"] == 7
    assert wf["3"]["inputs"]["sampler_name"] == "dpmpp_2m"
    assert wf["3"]["inputs"]["scheduler"] == "karras"
    assert wf["6"]["inputs"]["text"] == "hello"
    assert wf["7"]["inputs"]["text"] == "ugly"


def test_collect_image_refs_handles_missing_outputs() -> None:
    assert image_comfy._collect_image_refs({}) == []
    assert image_comfy._collect_image_refs({"outputs": {"3": {}}}) == []
    refs = image_comfy._collect_image_refs(
        {
            "outputs": {
                "9": {
                    "images": [
                        {"filename": "a.png", "subfolder": "", "type": "output"},
                        {"filename": "b.png", "subfolder": "sub", "type": "output"},
                    ]
                }
            }
        }
    )
    assert refs == [
        {"filename": "a.png", "subfolder": "", "type": "output"},
        {"filename": "b.png", "subfolder": "sub", "type": "output"},
    ]


def test_generate_image_registered_with_image_category() -> None:
    t = REGISTRY["generate_image"]
    assert t.category == "image"
    schema = t.openai_schema()["function"]
    assert "prompt" in schema["parameters"]["required"]
    assert schema["parameters"]["properties"]["negative_prompt"]["default"] == ""
