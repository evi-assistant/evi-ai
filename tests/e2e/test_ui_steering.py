"""E2E: say something mid-answer without throwing the work away (1.0.52).

While a turn runs, Send becomes **Queue**; a follow-up typed then shows up at
once as "queued — applies at the next step" and flips to "applied" when the
agent takes it between steps. If the turn ends before there is a next step,
the follow-up is sent as a normal message instead, so nothing is lost.

Deterministic against the fake backend (tests/e2e/conftest.py): a
`[e2e-hold]` prompt keeps the turn in flight until the test releases it, and a
`[e2e-tool]` prompt answers with one read-only tool call, which is what gives
the turn a next step.

The "queued" tag is drawn before POST /steer leaves the page, so the tag alone
doesn't mean the turn has the follow-up. Ending the turn on the tag races that
request: a late one gets {"queued": false} and steerMessage quietly falls back
to a plain send, which passes without touching the path under test. So
`_queue_follow_up` waits for the server's own "queued" before anything ends
the turn, and each follow-up test also runs with /steer held back on purpose
(`late-steer`) to keep it that way.

The resend also depends on how the stream reaches the browser. sendMessage
stops reading at Done, so the server hands an unapplied follow-up back
(`Steering` applied=false) BEFORE the turn's Done event. When it came after
Done, the UI saw it only if both frames landed in the same network read, and
~1-3% of real runs lost the follow-up. The resend tests pin the arrival with
`_pin_chat_stream` and cover both splits on purpose.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

# A fetch shim that re-chunks every /api/chat event stream before sendMessage's
# reader sees it. MODE "tail": everything from the Done (or Error) frame on
# arrives in ONE read. MODE "split": every SSE frame arrives in its own read.
# Every Steering event the server sends is also kept in window.__e2eSteering.
_SSE_SHIM = r"""
(() => {
  const MODE = __MODE__;
  const realFetch = window.fetch.bind(window);
  const enc = new TextEncoder();
  window.__e2eSteering = [];
  const events = (frame) => frame.split(/\r?\n/).flatMap((line) => {
    if (!line.startsWith('data:')) return [];
    try { return [JSON.parse(line.slice(5).trim())]; } catch { return []; }
  });
  const isEnd = (frame) => events(frame).some(
    (ev) => ev && (ev.kind === 'Done' || ev.kind === 'Error'));
  window.fetch = async function (input, init) {
    const res = await realFetch(input, init);
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const type = res.headers.get('content-type') || '';
    if (!/\/api\/chat$/.test(url.split('?')[0]) || !type.includes('text/event-stream')
        || !res.body) return res;
    const up = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    let tail = null;   // "tail" mode: the Done frame and everything after it
    const body = new ReadableStream({
      async pull(ctl) {
        for (;;) {
          const { done, value } = await up.read();
          if (done) {
            const rest = (tail || '') + buf + dec.decode();
            if (rest) ctl.enqueue(enc.encode(rest));
            ctl.close();
            return;
          }
          buf += dec.decode(value, { stream: true });
          let sep;
          let sent = 0;
          while ((sep = buf.match(/\r?\n\r?\n/)) !== null) {
            const frame = buf.slice(0, sep.index + sep[0].length);
            buf = buf.slice(frame.length);
            for (const ev of events(frame)) {
              if (ev && ev.kind === 'Steering') window.__e2eSteering.push(ev);
            }
            if (tail === null && MODE === 'tail' && isEnd(frame)) tail = '';
            if (tail !== null) { tail += frame; continue; }
            ctl.enqueue(enc.encode(frame));   // one chunk = one reader.read()
            sent += 1;
          }
          if (sent) return;
        }
      },
      cancel(reason) { return up.cancel(reason); },
    });
    return new Response(body, { status: res.status, statusText: res.statusText,
                                headers: res.headers });
  };
})();
"""


def _pin_chat_stream(page: Page, mode: str) -> None:
    """Fix how /api/chat's event stream is chunked for this page (see
    _SSE_SHIM). Installed before the page's own scripts, so the page's fetch
    wrapper (auth token, X-Evi-Client) still wraps every request."""
    assert mode in ("tail", "split"), mode
    page.add_init_script(_SSE_SHIM.replace("__MODE__", json.dumps(mode)))


# Holds every POST to .../steer back for DELAY_MS before it leaves the page, so
# the follow-up reaches the server late, as it now and then does under load.
_STEER_DELAY_SHIM = r"""
(() => {
  const DELAY_MS = __DELAY_MS__;
  const realFetch = window.fetch.bind(window);
  window.fetch = async function (input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    if (/\/steer$/.test(url.split('?')[0])) {
      await new Promise((resolve) => setTimeout(resolve, DELAY_MS));
    }
    return realFetch(input, init);
  };
})();
"""

# Each follow-up test runs with /steer as quick as it comes, and again with it
# held back well past the moment a test that didn't wait for it would end the
# turn: there, only `_queue_follow_up` keeps the steer ahead of the turn's end.
_STEER_TIMING = pytest.mark.parametrize(
    "steer_delay_ms", [0, 1500], ids=["prompt-steer", "late-steer"])


def _delay_steer(page: Page, ms: int) -> None:
    if ms:
        page.add_init_script(_STEER_DELAY_SHIM.replace("__DELAY_MS__", str(int(ms))))


def _open(page: Page, url: str) -> None:
    """Load the UI and let its page-load requests finish before a turn starts.

    The page's load-time GET /api/session/cwd and the first chat used to race
    to build the session, and the turn could run on an orphan that /steer
    called idle. get_session now keeps the first one stored for both
    (tests/test_session_race.py); settling first still keeps these tests to
    the one flow they are about."""
    page.goto(url)
    page.wait_for_load_state("networkidle")


def _history(page: Page) -> list[dict]:
    return page.evaluate("""async () =>
        (await (await fetch('/api/session/' + sessionId + '/history')).json()).messages""")


# The checks below that the test took the path it means to are pytest.fail,
# not assert, so a run that never reached the path under test reads as such.

def _start_held_turn(page: Page, fake_llm, prompt: str) -> None:
    page.fill("#input", prompt)
    page.click("#send")
    # The server is now inside the turn (the fake backend is holding it), so
    # a follow-up is a real mid-turn steer, not a race with the turn's start.
    if not fake_llm.held.wait(20):
        pytest.fail("the held turn never reached the fake backend")


def _queue_follow_up(page: Page, text: str) -> None:
    """Type `text` mid-turn and wait until the SERVER has queued it on the
    turn. The "queued" tag is drawn before POST /steer goes out, so a test
    that released the turn on the tag alone could have the request land after
    the turn ended, get {"queued": false}, and pass down steerMessage's plain
    send fallback instead of the steering path."""
    page.fill("#input", text)
    with page.expect_response(
        lambda r: r.request.method == "POST" and r.url.split("?")[0].endswith("/steer")
    ) as ack:
        page.press("#input", "Enter")
    verdict = ack.value.json()
    if verdict.get("queued") is not True:
        pytest.fail(f"the running turn did not queue the follow-up: {verdict}")


def test_send_becomes_queue_while_a_turn_runs(page: Page, evi_base_url: str, fake_llm):
    page.goto(evi_base_url)
    send = page.locator("#send")
    expect(send).to_have_text("Send")
    _start_held_turn(page, fake_llm, f"{fake_llm.HOLD} take your time")
    expect(send).to_have_text("Queue")
    expect(send).to_be_enabled()      # live on purpose: a follow-up steers the turn
    fake_llm.release.set()
    expect(page.locator("#log .msg.assistant").last).to_contain_text(
        "fake backend", timeout=20_000)
    expect(send).to_have_text("Send", timeout=10_000)


@_STEER_TIMING
def test_follow_up_is_queued_then_applied_at_the_next_step(
    page: Page, evi_base_url: str, fake_llm, steer_delay_ms: int
):
    _delay_steer(page, steer_delay_ms)
    _open(page, evi_base_url)
    _start_held_turn(page, fake_llm, f"{fake_llm.HOLD}{fake_llm.TOOL} look something up")

    _queue_follow_up(page, "also keep it short")
    steer = page.locator("#log .msg.user", has_text="also keep it short")
    expect(steer).to_have_count(1)
    expect(steer.locator(".steer-tag")).to_have_text("queued — applies at the next step")
    expect(page.locator("#input")).to_have_value("")

    # Release: the fake answers with a tool call, the tool runs, and the agent
    # takes the queued follow-up before its next model call.
    fake_llm.release.set()
    expect(steer.locator(".steer-tag")).to_have_text("applied", timeout=20_000)
    expect(page.locator("#log .msg.assistant").last).to_contain_text(
        "fake backend", timeout=20_000)
    expect(page.locator("#send")).to_have_text("Send", timeout=10_000)
    # Exactly one bubble for it — applied in place, not resent as a new turn.
    expect(steer).to_have_count(1)

    # Server-side it landed as a user message AFTER the tool result, i.e. at
    # the next step rather than interrupting the tool in flight.
    hist = _history(page)
    roles = [(m["role"], str(m["content"] or "")) for m in hist]
    tool_at = next(i for i, (r, _c) in enumerate(roles) if r == "tool")
    steer_at = next(i for i, (r, c) in enumerate(roles)
                    if r == "user" and "also keep it short" in c)
    assert steer_at > tool_at, roles
    assert any(fake_llm.TOOL_NAME in str(m.get("tool_calls") or "") for m in hist), roles


def _follow_up_outlives_the_turn(page: Page, fake_llm, follow_up: str) -> None:
    """Queue `follow_up` on a turn with no next step, end the turn, and check
    the UI resent it as a normal message with its own answer."""
    _start_held_turn(page, fake_llm, f"{fake_llm.HOLD} just answer")

    _queue_follow_up(page, follow_up)
    queued = page.locator("#log .msg.user .steer-tag")
    expect(queued).to_have_text("queued — applies at the next step")

    fake_llm.release.set()
    # Once the held turn's stream is done (Send is back), its Steering
    # applied=false has been handled or dropped: the tag must be gone by now.
    expect(page.locator("#send")).to_have_text("Send", timeout=20_000)
    expect(queued).to_have_count(0, timeout=3_000)
    # ...and it went because the server handed the follow-up back, which is
    # the resend under test, not steerMessage's own "not busy" fallback.
    handed_back = page.evaluate("window.__e2eSteering")
    if not any(ev.get("text") == follow_up and ev.get("applied") is False
               for ev in handed_back):
        pytest.fail(f"no Steering applied=false for the follow-up: {handed_back}")
    # The queued bubble was replaced by a normal user message that gets its
    # own answer: two user bubbles, two replies.
    expect(page.locator("#log .msg.assistant")).to_have_count(2, timeout=20_000)
    users = page.locator("#log .msg.user")
    expect(users).to_have_count(2)
    expect(users.nth(1)).to_contain_text(follow_up)
    expect(page.locator("#send")).to_have_text("Send", timeout=10_000)
    texts = [str(m["content"] or "") for m in _history(page) if m["role"] == "user"]
    assert sum(follow_up in t for t in texts) == 1, texts


@_STEER_TIMING
def test_follow_up_is_resent_when_the_turn_ends_first(
    page: Page, evi_base_url: str, fake_llm, steer_delay_ms: int
):
    """No tool call means no next step: the server hands the follow-up back
    (Steering applied=false) and the UI sends it as a normal message. Here
    Done and everything after it arrive in one read."""
    _pin_chat_stream(page, "tail")
    _delay_steer(page, steer_delay_ms)
    _open(page, evi_base_url)
    _follow_up_outlives_the_turn(page, fake_llm, "and one more thing")


@_STEER_TIMING
def test_follow_up_is_resent_when_its_frame_comes_after_done(
    page: Page, evi_base_url: str, fake_llm, steer_delay_ms: int
):
    """The same resend with every frame in its own read, Done included. When
    the hand-back came after Done, this lost the follow-up; it comes first
    now, so the UI has it before it stops reading."""
    _pin_chat_stream(page, "split")
    _delay_steer(page, steer_delay_ms)
    _open(page, evi_base_url)
    _follow_up_outlives_the_turn(page, fake_llm, "and one last thing")
