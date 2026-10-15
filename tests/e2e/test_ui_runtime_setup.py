"""E2E: the "Set up local AI" card (1.0.50).

Before anything downloads, the card lists what it will put on the machine (real
sizes, destination paths, loopback-only, no admin) with the literal URLs behind
"Show the exact files & commands". While it runs it names its steps (Prepare
runtime · Download model · Start the model · Ready), and every failure offers
**Run diagnostics** and **View log**, which expands the managed server's log in
place.

The runtime endpoints are stubbed in the browser (the real ones download a
runtime and a model, and size them with a request to GitHub). The log view
reads the real `/api/runtime/log`, from a file the test writes in the
server's tmp EVI_HOME.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

STARTER, BIG = "qwen2.5-1.5b", "qwen2.5-7b"
CATALOG = {
    "models": [
        {"id": BIG, "name": "Qwen2.5 7B Instruct", "size_gb": 4.7, "installed": True},
        {"id": STARTER, "name": "Qwen2.5 1.5B Instruct", "size_gb": 1.1, "installed": False},
    ],
    "recommended_id": BIG, "starter_id": STARTER, "active_model_id": None,
    "server_running": False,
}
RUNTIME_URL = "https://github.com/ggml-org/llama.cpp/releases/download/b0000/llama-cpu.zip"
MODEL_URL = "https://huggingface.co/e2e/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf"
IDLE = {"supported": True, "runtime_installed": False, "server_running": False,
        "stage": "idle", "message": "", "pct": None, "done": False, "error": ""}


def _plan(model_id: str) -> dict:
    have_model = model_id == BIG
    items = [
        {"kind": "runtime", "label": "llama.cpp runtime b0000",
         "detail": "CPU build — works on any machine, no drivers.", "size": "38.2 MB",
         "path": "/home/e2e/.evi/runtime", "needed": True},
        {"kind": "model", "label": f"{model_id} (Q4_K_M)",
         "detail": "Already downloaded." if have_model else "Small and fast.",
         "size": "" if have_model else "1.1 GB",
         "path": "/home/e2e/.evi/models", "needed": not have_model},
        {"kind": "server", "label": "A local server on 127.0.0.1",
         "detail": "Loopback only — not reachable from your network or the internet.",
         "size": "", "needed": True},
        {"kind": "admin", "label": "No administrator access needed",
         "detail": "Everything is written inside your user folder.", "size": "", "needed": False},
    ]
    cmds = [RUNTIME_URL] + ([] if have_model else [MODEL_URL])
    return {"model_id": model_id, "items": items, "commands": cmds, "external": "",
            "supported": True, "log_path": "/home/e2e/.evi/runtime/logs/llama-server.log"}


class _Runtime:
    """Browser-side stand-in for the runtime API; the test drives `status`."""

    def __init__(self, page: Page) -> None:
        self.status = dict(IDLE)
        self.plans: list[str] = []
        self.used: list[str] = []
        page.route("**/api/runtime/status", lambda r: r.fulfill(json=self.status))
        page.route("**/api/runtime/catalog", lambda r: r.fulfill(json=CATALOG))
        page.route("**/api/runtime/plan*", self._plan)
        page.route("**/api/runtime/model/use", self._use)

    def _plan(self, route) -> None:
        mid = route.request.url.split("model_id=", 1)[1]
        self.plans.append(mid)
        route.fulfill(json=_plan(mid))

    def _use(self, route) -> None:
        self.used.append(route.request.post_data_json["id"])
        route.fulfill(json={"ok": True})

    def set(self, **kw) -> None:
        self.status = {**self.status, **kw}


def _open_card(page: Page, evi_base_url: str) -> _Runtime:
    rt = _Runtime(page)
    page.goto(evi_base_url)
    page.evaluate("openLocalAISetup()")
    expect(page.locator("#dialog-title")).to_have_text("Set up local AI")
    expect(page.locator("#rt-go")).to_be_visible(timeout=10_000)
    return rt


def _step_marks(page: Page) -> list[str]:
    """['✓'|'◐'|'○', …] for Prepare runtime · Download model · Start · Ready."""
    return page.evaluate("""() => [...document.querySelectorAll('#rt-progress > div')]
        .slice(0, 4).map(d => d.querySelector('span').textContent.trim())""")


def test_first_run_download_option_opens_the_card(page: Page, evi_base_url: str):
    """The ladder's last rung hands over to this card."""
    page.route("**/api/backend/status", lambda r: r.fulfill(json={
        "configured": {"backend": "openai_compat", "base_url": "http://127.0.0.1:9/v1",
                       "model": "fake", "reachable": False},
        "candidates": [], "any_reachable": False, "runtime_supported": True,
        "suggestions": [{"kind": "llamacpp", "action": "runtime", "title": "Download a model",
                         "detail": "About 1 GB.", "badge": "key-free", "instant": False}],
    }))
    _Runtime(page)
    page.goto(evi_base_url)
    page.locator(".setup-opt", has_text="Download a model").click()
    expect(page.locator("#setup-screen")).to_have_count(0)
    expect(page.locator("#dialog-overlay")).to_be_visible()
    expect(page.locator("#dialog-title")).to_have_text("Set up local AI")
    expect(page.locator("#rt-go")).to_have_text("⚡ Download & start", timeout=10_000)


def test_card_lists_what_it_will_put_on_the_machine(page: Page, evi_base_url: str):
    rt = _open_card(page, evi_base_url)
    content = page.locator("#dialog-content")
    expect(content).to_contain_text("~40 MB")

    # The first-run pick is the small starter, not the bigger recommended one.
    model = page.locator("#rt-model")
    expect(model).to_have_value(STARTER)
    expect(model.locator(f'option[value="{BIG}"]')).to_contain_text("recommended for your PC")
    expect(model.locator(f'option[value="{BIG}"]')).to_contain_text("●")   # downloaded

    plan = page.locator("#rt-plan")
    expect(plan).to_contain_text("What this will put on your PC")
    expect(plan).to_contain_text("38.2 MB")
    expect(plan).to_contain_text("1.1 GB")
    expect(plan).to_contain_text("/home/e2e/.evi/models")
    expect(plan).to_contain_text("Loopback only")
    expect(plan).to_contain_text("No administrator access needed")
    # One row per item, its verb first: what downloads, what runs, what's a note.
    verbs = page.evaluate("""() => [...document.querySelectorAll(
        '#rt-plan > div:nth-child(2) > div')].map(row => row.firstElementChild.textContent)""")
    assert verbs == ["download", "download", "run", "note"], verbs

    # The literal URLs sit behind a disclosure.
    urls = plan.locator("details pre")
    expect(urls).to_be_hidden()
    plan.locator("summary", has_text="Show the exact files & commands").click()
    expect(urls).to_contain_text(RUNTIME_URL)
    expect(urls).to_contain_text(MODEL_URL)

    # Picking another model re-plans: an installed model is no longer a download.
    model.select_option(BIG)
    expect(plan).to_contain_text("Already downloaded.")
    expect(plan).not_to_contain_text("1.1 GB")
    expect(plan.locator("details pre")).not_to_contain_text(MODEL_URL)
    assert rt.plans[-1] == BIG and STARTER in rt.plans, rt.plans


def test_setup_names_its_steps(page: Page, evi_base_url: str):
    rt = _open_card(page, evi_base_url)
    page.locator("#rt-go").click()

    # Idle (nothing under way yet): every step pending, no spinner on step 1.
    expect(page.locator("#rt-progress")).to_contain_text("Prepare runtime", timeout=10_000)
    assert _step_marks(page) == ["○", "○", "○", "○"]
    assert rt.used == [STARTER]

    rt.set(stage="model", message="Downloading Qwen2.5 1.5B", pct=42)
    expect(page.locator("#rt-progress")).to_contain_text("Downloading Qwen2.5 1.5B")
    assert _step_marks(page) == ["✓", "◐", "○", "○"]

    rt.set(stage="starting", message="Loading the model", pct=None)
    expect(page.locator("#rt-progress")).to_contain_text("Loading the model")
    assert _step_marks(page) == ["✓", "✓", "◐", "○"]

    rt.set(stage="done", done=True, server_running=True)
    expect(page.locator("#rt-progress")).to_contain_text("Local AI is ready")
    assert _step_marks(page) == ["✓", "✓", "✓", "✓"]


def test_failure_offers_the_log_and_diagnostics(page: Page, evi_base_url: str, evi_home):
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    rt = _open_card(page, evi_base_url)
    log_path = Path(page.evaluate(
        "() => fetch('/api/runtime/log').then(r => r.json()).then(d => d.path)"))
    # The tmp home, never ~/.evi.
    assert log_path.resolve().is_relative_to(Path(evi_home).resolve()), log_path
    log_path.unlink(missing_ok=True)

    page.locator("#rt-go").click()
    rt.set(stage="model", error="download interrupted (disk full)")
    prog = page.locator("#rt-progress")
    expect(prog).to_contain_text("Setup failed: download interrupted (disk full)",
                                 timeout=10_000)
    view = prog.locator("#rt-log")
    expect(prog.locator("#rt-diag")).to_have_text("Run diagnostics")
    expect(view).to_have_text("View log")
    expect(page.locator("#rt-go")).to_be_enabled()          # retry stays available

    # No log yet: say why, rather than an empty box.
    view.click()
    expect(prog.locator("#rt-logbox")).to_contain_text("No log yet")
    expect(view).to_have_text("Hide log")
    view.click()
    expect(view).to_have_text("View log")
    expect(prog.locator("#rt-logbox")).to_be_empty()

    # A real log, served by the real server, expanded in place: the error and
    # the retry button are still there next to it.
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("main: loading model\nerror: unable to allocate 4.2 GB\n",
                        encoding="utf-8")
    try:
        view.click()
        box = prog.locator("#rt-logbox")
        expect(box.locator("pre")).to_contain_text("unable to allocate 4.2 GB")
        expect(box).to_contain_text(log_path.name)
        expect(prog).to_contain_text("Setup failed: download interrupted (disk full)")
        expect(page.locator("#rt-go")).to_be_visible()
    finally:
        log_path.unlink(missing_ok=True)

    # The second 1.0.49/1.0.50 dead button: it must open diagnostics.
    prog.locator("#rt-diag").click()
    expect(page.locator("#dialog-title")).to_have_text("Diagnostics")
    expect(page.locator("#dialog-content .set-about-row").first).to_be_visible(timeout=20_000)
    assert errors == [], errors
