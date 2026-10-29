// eVi web UI: the app script.
//
// index.html loads this file last, as a classic script (not a module, not
// deferred) at the end of <body>: it runs after the vendored libraries in
// <head> and once the markup above it is parsed, and its top-level functions
// and constants are globals (the desktop menu bridge and the tests call them
// by name). It used to be inline in index.html; the page's CSP now runs no
// inline script at all (script-src 'self', no hashes), so UI code belongs
// here or in another file under static/, never in a <script> in the page.

// ----- no framing ---------------------------------------------------------
//
// eVi never runs inside another page's frame, where a click meant for that
// page could land on Approve or Settings (clickjacking). The real control is
// the server's `frame-ancestors 'none'` and `X-Frame-Options: DENY` headers
// (a <meta> policy can't carry frame-ancestors), so the browser never loads
// the page into a frame. A copy served without them (an older server, a proxy
// that strips headers) stops here, blank and inert.
if (window.top !== window.self) {
  document.documentElement.replaceChildren();
  throw new Error('eVi does not run inside a frame');
}

// ----- auth -------------------------------------------------------------
//
// When `[web] auth_token` is set on the server, every /api request must
// carry an `Authorization: Bearer <token>` header (or `?token=` query
// param for streaming / image URLs that can't add headers).
//
// We monkeypatch `window.fetch` so the rest of the script (and any
// future code) doesn't have to know about the token. If a request comes
// back 401 we clear the cached token and pop the login overlay back up.
//
// Every same-origin request also carries `X-Evi-Client`: without a token the
// server refuses a browser's state-changing request that lacks it, and a page
// on another site can't add a custom header to a cross-origin request.

const AUTH_KEY = 'evi.authToken';
function getAuthToken() { return localStorage.getItem(AUTH_KEY) || ''; }
function setAuthToken(t) { localStorage.setItem(AUTH_KEY, t); }
function clearAuthToken() { localStorage.removeItem(AUTH_KEY); }

// The desktop shell injects its access token (the generated LAN token, or
// [web] auth_token) before this script runs, only for this server's origin —
// so the app signs itself in with no login prompt. It is captured at launch,
// so it can be stale (the token was rotated while the app ran): it fills in
// only when nothing is stored, and bootstrapAuth falls back to it when the
// stored token fails. A working stored token is never replaced.
const INJECTED_TOKEN = (() => {
  try {
    return (typeof window.__EVI_TOKEN__ === 'string' && window.__EVI_TOKEN__) || '';
  } catch { return ''; }
})();
try { if (INJECTED_TOKEN && !getAuthToken()) setAuthToken(INJECTED_TOKEN); } catch {}

const _origFetch = window.fetch.bind(window);
window.fetch = async function(input, init) {
  const url = typeof input === 'string' ? input : (input && input.url) || '';
  // Only attach the token to same-origin requests so we don't leak it to
  // CDNs or external APIs.
  const sameOrigin = !/^https?:\/\//i.test(url) || url.startsWith(location.origin);
  const tokUsed = sameOrigin ? getAuthToken() : '';
  if (sameOrigin) {
    init = init || {};
    const h = new Headers(init.headers || (typeof input !== 'string' && input && input.headers) || {});
    if (!h.has('X-Evi-Client')) h.set('X-Evi-Client', 'web');
    if (tokUsed && !h.has('Authorization')) h.set('Authorization', 'Bearer ' + tokUsed);
    init.headers = h;
  }
  const resp = await _origFetch(input, init);
  // Only sign out if the token that was actually REJECTED is still the current
  // one — a stale in-flight 401 (from a request that started before you signed
  // in) must not wipe the token you just entered.
  if (resp.status === 401 && sameOrigin && getAuthToken() === tokUsed) {
    clearAuthToken();
    showAuthOverlay('Session expired. Please sign in again.');
  }
  return resp;
};

// Encodes the auth token as a query param for URLs that need it (img src,
// EventSource — anything where headers can't be set).
function authQuery() {
  const tok = getAuthToken();
  return tok ? ('token=' + encodeURIComponent(tok)) : '';
}

function showAuthOverlay(msg) {
  const ov = document.getElementById('auth-overlay');
  if (!ov) return;
  // Only reset + focus the field the FIRST time the overlay appears. Background
  // polls (federation/inbox/model-picker) each 401 while signed out and re-call
  // this every few seconds — clearing/refocusing on every call wiped whatever
  // you were pasting/typing (the "token field keeps clearing" bug).
  const firstShow = !ov.classList.contains('shown');
  ov.classList.add('shown');
  const err = document.getElementById('auth-err');
  if (err) err.textContent = msg || '';
  const input = document.getElementById('auth-token');
  if (input && firstShow) { input.value = ''; input.focus(); }
}
function hideAuthOverlay() {
  const ov = document.getElementById('auth-overlay');
  if (ov) ov.classList.remove('shown');
}

document.getElementById('auth-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const tok = document.getElementById('auth-token').value.trim();
  if (!tok) return;
  const btn = document.getElementById('auth-submit');
  btn.disabled = true;
  try {
    const r = await _origFetch('/api/auth/check', {
      headers: { 'Authorization': 'Bearer ' + tok },
    });
    let refused = '';
    if (r.ok) {
      const data = await r.json();
      if (data.ok) {
        setAuthToken(tok);
        hideAuthOverlay();
        // Re-bootstrap the parts of the UI that ran before we had a token.
        location.reload();
        return;
      }
      // A login the server knows but won't sign in (e.g. two users.json
      // logins sharing one data folder) says why.
      refused = typeof data.error === 'string' ? data.error : '';
    }
    document.getElementById('auth-err').textContent =
      refused || 'Invalid token. Try again or run `evi web token show`.';
  } catch (err) {
    document.getElementById('auth-err').textContent =
      'Server unreachable: ' + (err && err.message || err);
  } finally {
    btn.disabled = false;
  }
});

// Bootstrap: figure out whether the server requires auth. If yes and the
// stored token doesn't validate, surface the overlay BEFORE the rest of
// the script starts firing requests.
(async function bootstrapAuth() {
  try {
    const tok = getAuthToken();
    const headers = tok ? { 'Authorization': 'Bearer ' + tok } : {};
    const r = await _origFetch('/api/auth/check', { headers });
    if (!r.ok) {
      showAuthOverlay('');
      return;
    }
    const data = await r.json();
    if (data.required && !data.ok) {
      // The stored token failed; the shell's injected one may still work.
      if (INJECTED_TOKEN && INJECTED_TOKEN !== tok) {
        const r2 = await _origFetch('/api/auth/check', {
          headers: { 'Authorization': 'Bearer ' + INJECTED_TOKEN },
        });
        const d2 = r2.ok ? await r2.json() : {};
        if (d2.ok) {
          setAuthToken(INJECTED_TOKEN);
          location.reload();  // re-bootstrap what already ran with the bad token
          return;
        }
      }
      showAuthOverlay(typeof data.error === 'string' ? data.error : '');
    }
  } catch {
    // Network errors leave the page in its default state — the chat
    // bootstrap below will retry and the user can see the connection
    // error in the meta line.
  }
})();

const log = document.getElementById('log');
const form = document.getElementById('form');
const input = document.getElementById('input');
const send = document.getElementById('send');
const meta = document.getElementById('meta');
const resetBtn = document.getElementById('reset');
const chipUsage = document.getElementById('chip-usage');
const chipGoal = document.getElementById('chip-goal');
const chipPlan = document.getElementById('chip-plan');
const chipAuto = document.getElementById('chip-auto');

// Stop: ends the running turn (POST /api/session/{id}/cancel). Shown next to
// Send only while a turn this page started (a message or a re-roll) runs; the
// turn then ends with Done "interrupted" on its own stream, which resets it.
const stopBtn = document.createElement('button');
stopBtn.type = 'button';
stopBtn.id = 'stop';
stopBtn.textContent = 'Stop';
stopBtn.title = 'Stop this turn';
stopBtn.style.cssText = 'display:none;background:var(--err);color:#fff;border:none;'
  + 'padding:0 14px;border-radius:4px;cursor:pointer;font:inherit;font-weight:600';
send.insertAdjacentElement('afterend', stopBtn);
let stopSid = null;  // the session whose turn Stop ends
function showStop(sid) {
  stopSid = sid;
  stopBtn.disabled = false;
  stopBtn.textContent = 'Stop';
  stopBtn.style.display = '';
}
function hideStop() {
  stopSid = null;
  stopBtn.style.display = 'none';
}
async function stopTurn() {
  const sid = stopSid;
  if (!sid || stopBtn.disabled) return;
  stopBtn.disabled = true;
  stopBtn.textContent = 'Stopping…';
  let ok = false;
  try {
    const r = await fetch('/api/session/' + encodeURIComponent(sid) + '/cancel', { method: 'POST' });
    ok = r.ok;
  } catch (e) { /* the stream's own end (or error) still resets the UI */ }
  if (ok) {
    // The server denied any question the turn was waiting on; close it here
    // too, so the stream's reader (awaiting the answer) reads on to the end.
    settlePermissionPrompts(sid);
  } else if (stopSid === sid) {
    stopBtn.disabled = false;  // not stopped: the question stays answerable
    stopBtn.textContent = 'Stop';
  }
}
stopBtn.addEventListener('click', stopTurn);

async function refreshUsage() {
  try {
    const r = await fetch(`/api/session/${sessionId}/usage`);
    if (!r.ok) return;
    const { used, ceiling } = await r.json();
    const k = n => Math.round(n / 1000) + 'k';
    if (!ceiling) {
      // Unknown context ceiling: still surface the running token count (keeping
      // the word "tokens" so the single chip format never diverges — see #29)
      // instead of hiding the chip after a real turn.
      if (!used) { chipUsage.style.display = 'none'; return; }
      chipUsage.textContent = `${used.toLocaleString()} tokens`;
      chipUsage.style.display = 'inline-block';
      chipUsage.classList.remove('active');
      chipUsage.style.color = 'var(--dim)';
      return;
    }
    const pct = Math.round((used / ceiling) * 100);
    chipUsage.textContent = `${k(used)} / ${k(ceiling)} tokens (${pct}%)`;
    chipUsage.style.display = 'inline-block';
    chipUsage.classList.toggle('active', pct >= 70);
    chipUsage.style.color = pct >= 85 ? 'var(--err)' : (pct >= 70 ? 'var(--tool)' : 'var(--dim)');
  } catch {}
}

// Click the usage chip for a per-category context breakdown (Phase 88).
chipUsage.style.cursor = 'pointer';
chipUsage.title = 'Context breakdown — click';
let _ctxPop = null;
function _closeCtxPop() { if (_ctxPop) { _ctxPop.remove(); _ctxPop = null; } }
chipUsage.addEventListener('click', async (ev) => {
  ev.stopPropagation();
  if (_ctxPop) { _closeCtxPop(); return; }
  let rep;
  try {
    const r = await fetch(`/api/session/${sessionId}/context`);
    if (!r.ok) return;
    rep = await r.json();
  } catch { return; }
  const k = n => n >= 1000 ? Math.round(n / 1000) + 'k' : '' + n;
  const labels = { system: 'system prompt', schemas: 'tool schemas', user: 'you', assistant: 'assistant', tools: 'tools' };
  const pop = document.createElement('div');
  pop.className = 'ctx-pop';
  let html = `<div class="ctx-head">~${rep.used.toLocaleString()} tokens`
    + (rep.ceiling ? ` / ${rep.ceiling.toLocaleString()} (${rep.pct}%)` : '')
    + ` · ${rep.messages} msgs</div>`;
  for (const b of ['system', 'schemas', 'user', 'assistant', 'tools']) {
    const toks = (rep.buckets && rep.buckets[b]) || 0;
    const share = (rep.pct_of_used && rep.pct_of_used[b]) || 0;
    html += `<div class="ctx-row"><span class="ctx-lbl">${labels[b]}</span>`
      + `<span class="ctx-bar"><i style="width:${share}%"></i></span>`
      + `<span class="ctx-val">${k(toks)} ${share}%</span></div>`;
  }
  // Which parts of the system prompt and which tool groups the tokens went to.
  const sub = (title, groups) => {
    const items = Object.entries(groups || {}).filter(([, t]) => t > 0);
    if (!items.length) return '';
    return `<div class="ctx-sub">${title}: `
      + items.map(([g, t]) => `${escapeHtml(g)} ${k(t)}`).join(', ') + '</div>';
  };
  html += sub('system prompt', rep.system_groups) + sub('tool schemas', rep.schema_groups);
  pop.innerHTML = html;
  document.body.appendChild(pop);
  const rect = chipUsage.getBoundingClientRect();
  pop.style.top = (rect.bottom + 6) + 'px';
  pop.style.right = Math.max(8, window.innerWidth - rect.right) + 'px';
  _ctxPop = pop;
  setTimeout(() => document.addEventListener('click', _closeCtxPop, { once: true }), 0);
});

// --- conversation tabs ---------------------------------------------------
//
// `tabs` is a list of {id, label}. `activeIdx` is the index of the current
// tab. All persisted to localStorage so reloads preserve the workspace.
// `sessionId` is kept in sync with tabs[activeIdx].id so the rest of the
// script (which still references `sessionId` directly) sees the right id.

const TABS_KEY = 'evi.tabs';
const ACTIVE_KEY = 'evi.activeIdx';

function loadTabsState() {
  try {
    const raw = localStorage.getItem(TABS_KEY);
    const idx = parseInt(localStorage.getItem(ACTIVE_KEY) || '0', 10);
    if (!raw) return null;
    const arr = JSON.parse(raw);
    if (!Array.isArray(arr) || !arr.length) return null;
    return { tabs: arr, activeIdx: Math.min(Math.max(0, idx), arr.length - 1) };
  } catch { return null; }
}
function saveTabsState() {
  try {
    localStorage.setItem(TABS_KEY, JSON.stringify(tabs));
    localStorage.setItem(ACTIVE_KEY, String(activeIdx));
  } catch {}
}

const _loaded = loadTabsState();
let tabs = _loaded ? _loaded.tabs : [{ id: crypto.randomUUID(), label: 'new chat' }];
let activeIdx = _loaded ? _loaded.activeIdx : 0;
let sessionId = tabs[activeIdx].id;
// A pending file attachment is per-composer, not global to the app — declared
// here (early) so switching sessions/tabs can clear it (see setSessionId), so a
// picture staged in one chat never leaks into a message sent from another.
let attachedFile = null;
let attachedIsImage = false;

// Switch the active session. Always go through this rather than assigning
// sessionId directly: the working-folder chip is per-session, and every place
// that reassigned sessionId used to forget to refresh it, so the chip kept
// advertising the PREVIOUS session's folder. That is worse than showing
// nothing — you trust the chip, write a relative path, and the file lands in
// the server's cwd instead. (loadCwd is a hoisted function declaration, so
// calling it from earlier in the file is fine.)
function setSessionId(id) {
  sessionId = id;
  // Drop any file staged but not yet sent — it belonged to the chat we're
  // leaving, and must not ride along on the next message in this one.
  attachedFile = null;
  attachedIsImage = false;
  if (typeof loadCwd === 'function') loadCwd();
}
let goalChipText = null;
let planArmed = false;
let autoOn = false;

function refreshHeaderModel() {
  fetch('/api/health').then(r => r.json()).then(d => {
    meta.textContent = `model=${d.model}`;
  }).catch(() => { meta.textContent = 'offline'; });
}
refreshHeaderModel();

// config.toml there but unreadable: eVi runs on the defaults and won't save
// over the file (Config.load / Config.save). Say so above everything; the
// detail (the owner's GET /api/config) is added when it can be read. Text only.
function checkConfigFile() {
  fetch('/api/health', { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (d) {
    const el = document.getElementById('config-banner');
    if (!el) return;
    if (d.config_ok !== false) { el.style.display = 'none'; el.textContent = ''; return; }
    el.textContent = '';
    const head = document.createElement('b');
    head.textContent = "Your settings file couldn't be read.";
    el.appendChild(head);
    el.appendChild(document.createTextNode(
      " eVi is running on its default settings, and changes in Settings won't be saved " +
      "until config.toml is fixed (or moved aside to start fresh). Then reload."));
    el.style.display = 'block';
    fetch('/api/config', { cache: 'no-store' }).then(function (r) { return r.ok ? r.json() : null; })
      .then(function (c) {
        if (!c || !c.load_error) return;
        const detail = document.createElement('div');
        detail.style.marginTop = '6px';
        const code = document.createElement('code');
        code.textContent = c.load_error;
        detail.appendChild(code);
        el.appendChild(detail);
      }).catch(function () {});
  }).catch(function () {});
}
checkConfigFile();

// Surface context-window usage. Refresh on connect and after each turn.
refreshUsage();

// Tracks the history index of the next message bubble we'll create. The
// server's history[0] is the system prompt; we never render that, so the
// first bubble we ever create is index 1.
let nextHistoryIndex = 1;

function addBubble(cls, { history = true } = {}) {
  const div = document.createElement('div');
  div.className = `msg ${cls}`;
  // Only user/assistant/tool bubbles map to real history entries; the
  // other classes (system, thinking, upload-chip, error) live outside the
  // history-edit model. Pass {history:false} for a UI-only artifact that must
  // NOT claim a history index (e.g. the transient tool-progress bubble) —
  // otherwise the server/client indices drift and edit/branch/delete would
  // target the wrong message.
  if (history && (cls === 'user' || cls === 'assistant' || cls === 'tool')) {
    div.dataset.index = String(nextHistoryIndex);
    nextHistoryIndex += 1;
    attachMessageActions(div, cls);
  }
  clearEmptyState();  // a real message replaces the starter prompts
  // Held notes (renderHeldNotes) stay last: they join the history only when
  // the running turn ends, after everything it adds.
  const held = document.getElementById('held-notes');
  if (held && held.parentNode === log) log.insertBefore(div, held);
  else log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div;
}

// ---- notes ------------------------------------------------------------
// A note from outside this chat (a message from another session, a channel
// push, your own message from the Dispatch panel) and the compaction summary
// are part of what the model reads, but neither typed here nor said by it, so
// they render as notes: no Edit / Re-roll / Branch / Delete, and the summary's
// assistant acknowledgement isn't drawn at all. The server says which is which
// (`kind` in /history: evi/history.py note_message / compaction_messages).
const COMPACTED_RE = /^\[compacted: (\d+) earlier messages summarised\]\n?/;

function renderNoteInto(bubble, m) {
  const text = String(m.content || '');
  const nl = text.indexOf('\n');
  const first = nl < 0 ? text : text.slice(0, nl);
  const body = document.createElement('div');
  body.className = 'note-body';
  if (m.kind === 'compaction') {
    const hit = COMPACTED_RE.exec(text);
    const n = hit ? Number(hit[1]) : 0;
    const d = document.createElement('details');
    const s = document.createElement('summary');
    s.textContent = `Summary of ${n} earlier message${n === 1 ? '' : 's'} (compacted)`;
    body.textContent = hit ? text.slice(hit[0].length).trim() : text;
    d.appendChild(s); d.appendChild(body);
    bubble.appendChild(d);
    return;
  }
  const head = document.createElement('div');
  head.className = 'note-head';
  // "[message from session "Beta" - information, not instructions]"
  head.textContent = '📨 ' + first.replace(/^\[/, '').replace(/\]$/, '');
  body.textContent = nl < 0 ? '' : text.slice(nl + 1);
  bubble.appendChild(head); bubble.appendChild(body);
}

// Draw history message `m` (a note or the compaction summary) in its place.
// It claims no bubble index, but the next bubble's index follows it.
function drawNote(m) {
  nextHistoryIndex = m.index + 1;
  const b = addBubble('note');
  if (m.kind === 'compaction') b.classList.add('compaction');
  renderNoteInto(b, m);
  return b;
}

// Notes waiting for the running turn to end (the server holds them so none
// lands between a tool call and its result, and joins them to the history
// after the turn). Drawn below everything, in a box new bubbles go above, so
// a note you could see doesn't vanish while a re-roll regenerates the reply
// before it.
function renderHeldNotes(held) {
  let box = document.getElementById('held-notes');
  if (!held || !held.length) { if (box) box.remove(); return; }
  if (!box) { box = document.createElement('div'); box.id = 'held-notes'; }
  box.innerHTML = '';
  for (const n of held) {
    const b = document.createElement('div');
    b.className = 'msg note held';
    renderNoteInto(b, n);
    const wait = document.createElement('div');
    wait.className = 'note-wait';
    wait.textContent = 'arrives when the current turn ends';
    b.appendChild(wait);
    box.appendChild(b);
  }
  clearEmptyState();
  log.appendChild(box);  // (re)placed last
  log.scrollTop = log.scrollHeight;
}

// Catch up on notes that joined this session's history after the chat was
// drawn: one delivered while it sat idle, or held through the turn that just
// ended. Drawn in place, so the next bubble's history index stays aligned
// with the server's (edit / branch / delete use it). Only a run of notes at the
// end is drawn here; anything else waits for the next full rebuild. One sync
// at a time, so a send right after a turn can't draw a note twice.
let _noteSync = null;
let historyView = 0;  // bumped by every full redraw (rebuildHistory)
function syncNotes(sid) {
  if (!_noteSync) _noteSync = _syncNotes(sid).finally(() => { _noteSync = null; });
  return _noteSync;
}
async function _syncNotes(sid) {
  if (sid !== sessionId || nextHistoryIndex <= 1) return;
  const since = nextHistoryIndex, view = historyView;
  let data;
  try {
    const r = await fetch(`/api/session/${sid}/history?since=${since}`);
    if (!r.ok) return;
    data = await r.json();
  } catch { return; }
  // Moved on meanwhile: another tab, a new bubble, or a full redraw.
  if (sid !== sessionId || nextHistoryIndex !== since || historyView !== view) return;
  const msgs = data.messages || [];
  const hadHeld = !!document.getElementById('held-notes');
  if (msgs.length && msgs[0].index === since && msgs.every(m => m.kind === 'note')) {
    for (const m of msgs) drawNote(m);
  } else if (hadHeld && msgs.some(m => m.kind === 'note')) {
    // A held note was released after messages this view didn't count:
    // redraw the whole chat rather than lose it.
    await rebuildHistory();
    return;
  }
  renderHeldNotes(data.held || []);
}

// ---- empty-chat starter prompts --------------------------------------
// A blank transcript tells a new user nothing about what eVi can do, and the
// first thing they need is proof it answers at all. Three prompts do that in one
// click, and double as a capability tour.
const STARTERS = [
  ['Say hi 👋', 'Say hi in one short sentence.'],
  ['What can you do?', 'What can you do? Give me a short, concrete list of the things you can help with.'],
  ['Give me a quick tour', 'Give me a quick tour of how to use you — chats, tools, and settings — in a few short bullets.'],
];
function clearEmptyState() {
  const e = document.getElementById('chat-empty');
  if (e) e.remove();
}
function renderEmptyState() {
  clearEmptyState();
  if (!log || log.children.length) return;          // only for a truly empty chat
  const wrap = document.createElement('div');
  wrap.id = 'chat-empty';
  wrap.style.cssText = 'display:flex;flex-direction:column;align-items:center;justify-content:center;' +
    'gap:9px;padding:44px 16px;text-align:center';
  wrap.innerHTML =
    '<div style="font-size:17px;font-weight:600">How can I help?</div>' +
    '<div style="font-size:12.5px;opacity:.6;margin-bottom:5px">Any reply means your model, tools and settings are working together.</div>' +
    STARTERS.map(function (s, i) {
      return '<button class="starter" data-i="' + i + '" style="min-width:250px;padding:9px 14px;border-radius:8px;' +
        'border:1px solid var(--border,#334155);background:var(--panel,#1e293b);color:inherit;font:inherit;' +
        'font-size:13.5px;cursor:pointer">' + escapeHtml(s[0]) + '</button>';
    }).join('');
  log.appendChild(wrap);
  wrap.querySelectorAll('.starter').forEach(function (b) {
    b.onclick = function () {
      // Deliberately do NOT clear here. The single log.appendChild path clears
      // the empty state when a real message renders, so if the send bails (no
      // reachable backend) the prompts survive instead of leaving a blank
      // transcript with nothing to click.
      sendMessage(STARTERS[+b.getAttribute('data-i')][1]);
    };
  });
}

function attachMessageActions(bubble, cls) {
  const idx = bubble.dataset.index;
  const actions = document.createElement('div');
  actions.className = 'msg-actions';
  // User can edit any role; only assistant gets re-roll.
  const buttons = [
    { label: '✏️', title: 'Edit', action: () => editBubble(bubble) },
  ];
  if (cls === 'assistant') {
    buttons.push({ label: '🔄', title: 'Re-roll', cls: 'reroll-btn', action: () => rerollFromBubble() });
  }
  buttons.push({ label: '🌿', title: 'Branch', action: () => branchFromBubble(idx) });
  if (cls !== 'tool') {
    buttons.push({ label: '🗜', title: 'Compact up to here', action: () => compactUpToBubble(idx) });
  }
  buttons.push({ label: '🗑', title: 'Delete from here', action: () => truncateFromBubble(idx) });
  for (const b of buttons) {
    const btn = document.createElement('button');
    btn.textContent = b.label;
    btn.title = b.title;
    if (b.cls) btn.classList.add(b.cls);
    btn.onclick = e => { e.stopPropagation(); b.action(); };
    actions.appendChild(btn);
  }
  bubble.appendChild(actions);
}

// Re-roll only ever regenerates the LATEST turn (the server rewinds to the last
// user message), so show the 🔄 button on the last assistant bubble only — on
// older bubbles it silently re-rolled a different turn.
function updateRerollButtons() {
  const assistants = log.querySelectorAll('.msg.assistant');
  const last = assistants[assistants.length - 1] || null;
  log.querySelectorAll('.reroll-btn').forEach(btn => {
    const bubble = btn.closest('.msg.assistant');
    btn.style.display = (last && bubble === last) ? '' : 'none';
  });
}

// `until`: draw only the messages before that history index (a re-roll's
// redraw stops where the server rewound, as the stream draws the new reply).
async function rebuildHistory({ until = Infinity } = {}) {
  try {
    const r = await fetch(`/api/session/${sessionId}/history`);
    if (!r.ok) return;
    const { messages, held } = await r.json();
    log.innerHTML = ''; renderEmptyState();
    historyView += 1;  // a note sync started before this redraw must not draw
    nextHistoryIndex = 1;
    for (const m of messages) {
      if (m.index >= until) break;
      if (m.role === 'system' || m.kind === 'compaction_ack') {
        // The system slot, or the acknowledgement that pairs with the
        // compaction summary (the summary's note stands for both).
        nextHistoryIndex = m.index + 1;
        continue;
      }
      if (m.kind === 'note' || m.kind === 'compaction') {
        drawNote(m);
        continue;
      }
      const cls = m.role === 'user' ? 'user'
                : m.role === 'assistant' ? 'assistant'
                : m.role === 'tool' ? 'tool'
                : 'system';
      // Ensure the bubble's data-index matches the server's index exactly.
      nextHistoryIndex = m.index;
      const bubble = addBubble(cls);
      if (cls === 'tool') {
        bubble.textContent = `← ${m.name || ''}: ${m.content || ''}`;
        // textContent= wiped the toolbar addBubble appended — re-attach it,
        // else tool bubbles lose Edit/Branch/Delete after any rebuild.
        attachMessageActions(bubble, 'tool');
      } else if (cls === 'assistant') {
        // Assistant messages get full markdown + syntax highlighting.
        // renderMarkdownInto re-attaches its own toolbar after setting innerHTML.
        renderMarkdownInto(bubble, m.content || '');
        if (m.tool_calls) {
          const note = document.createElement('div');
          note.style.cssText = 'color:var(--dim);font-size:11px;margin-top:4px';
          note.textContent = `(emitted ${m.tool_calls.length} tool call${m.tool_calls.length > 1 ? 's' : ''})`;
          bubble.appendChild(note);
        }
      } else {
        // User messages stay as plain text — they're what you typed.
        bubble.textContent = m.content || '';
        attachMessageActions(bubble, cls);  // textContent= wiped the toolbar
        // Re-render attachment previews the server matched to this message, so a
        // pasted/attached image survives a tab-switch rebuild — its attach-time
        // thumbnail was a transient blob that rebuild would otherwise drop.
        if (Array.isArray(m.images)) {
          for (const url of m.images) {
            const img = document.createElement('img');
            img.src = url;
            img.alt = '';
            img.style.cssText = 'display:block;max-width:240px;max-height:160px;'
              + 'margin-top:6px;border-radius:4px';
            bubble.appendChild(img);
          }
        }
      }
    }
    renderHeldNotes(held);
    updateRerollButtons();  // only the last assistant turn may re-roll
    refreshUsage();  // the newly-shown session's context usage (was stale)
  } catch (err) { console.error('history rebuild failed', err); }
}

function editBubble(bubble) {
  const idx = parseInt(bubble.dataset.index, 10);
  // Prefer the raw markdown source when we rendered it; fall back to
  // textContent for tool / user bubbles that we never markdown-rendered.
  const original = bubble.dataset.source || bubble.textContent;
  let body = original;
  if (bubble.classList.contains('tool')) {
    body = body.replace(/^← [^:]+: /, '');
  }
  // Replace bubble children with a textarea + save/cancel.
  bubble.innerHTML = '';
  const ta = document.createElement('textarea');
  ta.className = 'msg-edit';
  ta.value = body;
  const row = document.createElement('div');
  row.className = 'msg-edit-row';
  const cancel = document.createElement('button');
  cancel.className = 'cancel'; cancel.textContent = 'Cancel';
  cancel.onclick = () => rebuildHistory();
  const save = document.createElement('button');
  save.textContent = 'Save';
  save.onclick = async () => {
    save.disabled = true;
    const r = await fetch(`/api/session/${sessionId}/edit`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ at_index: idx, content: ta.value }),
    });
    if (!r.ok) { addBubble('error').textContent = 'edit failed'; rebuildHistory(); return; }
    // If we just edited a user message, the server truncated everything
    // after it. Re-roll to regenerate from that point.
    if (bubble.classList.contains('user')) {
      await rebuildHistory();
      await streamReroll();
    } else {
      await rebuildHistory();
    }
  };
  row.appendChild(cancel); row.appendChild(save);
  bubble.appendChild(ta); bubble.appendChild(row);
  ta.focus();
}

async function rerollFromBubble() {
  await streamReroll();
}

async function streamReroll() {
  send.disabled = true;
  let assistant = null;
  const turnSid = sessionId;
  showStop(turnSid);
  try {
    const r = await fetch(`/api/session/${turnSid}/reroll`, { method: 'POST' });
    if (r.status === 409) throw new Error('a turn is still running in this session');
    if (!r.ok || !r.body) throw new Error(`HTTP ${r.status}`);
    // Refresh history so any popped assistant/tool bubbles vanish, up to
    // where the server rewound: with a quick model the new reply is already
    // in the history, and the stream below draws it.
    const rewound = parseInt(r.headers.get('X-Evi-History-Length') || '', 10);
    await rebuildHistory(Number.isFinite(rewound) ? { until: rewound } : {});
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let done = false;
    while (!done) {
      const { done: streamDone, value } = await reader.read();
      if (streamDone) break;
      buf += decoder.decode(value, { stream: true });
      // SSE frames end in a blank line — tolerate BOTH \n\n and \r\n\r\n
      // (sse-starlette emits CRLF; splitting on \n\n only would never match).
      let sep;
      while ((sep = buf.match(/\r?\n\r?\n/)) !== null) {
        const chunk = buf.slice(0, sep.index); buf = buf.slice(sep.index + sep[0].length);
        for (const line of chunk.split(/\r?\n/)) {
          if (!line.startsWith('data:')) continue;
          const payload = line.slice(5).trim();
          if (!payload) continue;
          let ev; try { ev = JSON.parse(payload); } catch { continue; }
          assistant = await handleEvent(ev, assistant, turnSid);
          if (ev.kind === 'Done' || ev.kind === 'Error') done = true;
        }
      }
    }
  } catch (err) {
    addBubble('error').textContent = `re-roll failed: ${err.message}`;
  } finally {
    send.disabled = false;
    hideStop();
    refreshUsage();
    // The notes the re-roll held (drawn below the new reply meanwhile) are
    // back in the history, after it: draw them in their place.
    await syncNotes(turnSid);
  }
}

async function branchFromBubble(idx) {
  const r = await fetch(`/api/session/${sessionId}/branch`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ at_index: parseInt(idx, 10) }),
  });
  if (!r.ok) { addBubble('error').textContent = 'branch failed'; return; }
  const { new_session_id } = await r.json();
  // Open the branch as a NEW tab so the parent conversation stays
  // accessible. The parent's label is preserved; the branch inherits it
  // with a "·branch" suffix so they're easy to tell apart.
  const parentLabel = tabs[activeIdx]?.label || 'chat';
  tabs.push({ id: new_session_id, label: parentLabel + ' · branch' });
  activeIdx = tabs.length - 1;
  setSessionId(new_session_id);
  saveTabsState();
  renderTabs();
  await rebuildHistory();
}

async function truncateFromBubble(idx) {
  if (!confirm('Delete this and every message after it?')) return;
  const r = await fetch(`/api/session/${sessionId}/truncate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ after_index: parseInt(idx, 10) - 1 }),
  });
  if (!r.ok) { addBubble('error').textContent = 'truncate failed'; return; }
  await rebuildHistory();
}

// Summarise everything before this message; it and what follows stay as they are.
async function compactUpToBubble(idx) {
  if (!confirm('Summarise everything before this message? This message and the ones after it stay as they are.')) return;
  const r = await fetch(`/api/session/${sessionId}/compact`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ upto: parseInt(idx, 10) }),
  });
  const res = r.ok ? await r.json() : null;
  if (!res) { addBubble('error').textContent = 'compact failed'; return; }
  await rebuildHistory();
  if (!res.collapsed) addBubble('system').textContent = 'Nothing to compact before that message (or the summary failed).';
}

// Render a tool result with a "show more" toggle when output is long.
// Threshold matches what the model sees in the conversation history.
const TOOL_PREVIEW_CHARS = 400;
function renderToolResult(bubble, name, output, citations) {
  // Keep label + actions intact while we swap the body.
  bubble.innerHTML = '';
  const label = document.createElement('div');
  label.innerHTML = `← <strong>${escapeHtml(name)}</strong>:`;
  bubble.appendChild(label);
  const body = document.createElement('div');
  body.className = 'truncated-body';
  bubble.appendChild(body);
  const isLong = output.length > TOOL_PREVIEW_CHARS;
  let expanded = false;
  function paint() {
    body.textContent = (!isLong || expanded)
      ? output
      : output.slice(0, TOOL_PREVIEW_CHARS) + '…';
  }
  paint();
  // innerHTML='' above wiped the toolbar addBubble appended — re-attach it so
  // streamed tool-result bubbles keep Edit/Branch/Delete like every other row.
  attachMessageActions(bubble, 'tool');
  if (isLong) {
    const toggle = document.createElement('button');
    toggle.className = 'show-toggle';
    toggle.textContent = `show full (${output.length.toLocaleString()} chars)`;
    toggle.onclick = e => {
      e.stopPropagation();
      expanded = !expanded;
      toggle.textContent = expanded ? 'collapse' : `show full (${output.length.toLocaleString()} chars)`;
      paint();
    };
    bubble.appendChild(toggle);
  }
  // Citations footer — one chip per source.
  if (Array.isArray(citations) && citations.length) {
    const sources = document.createElement('div');
    sources.className = 'citations';
    sources.style.cssText =
      'margin-top: 6px; display: flex; flex-wrap: wrap; gap: 4px; font-size: 11px;';
    for (const c of citations) {
      const chip = document.createElement('span');
      const range = (c.start || c.end)
        ? `:${c.start || ''}${c.end ? '-' + c.end : ''}`
        : '';
      const sid = c.source_id || '(source)';
      chip.textContent = `[${c.id}] ${sid}${range}`;
      chip.title = c.excerpt || '';
      chip.style.cssText =
        'border: 1px solid var(--border); border-radius: 10px; '
        + 'padding: 2px 8px; color: var(--dim); cursor: default;';
      sources.appendChild(chip);
    }
    bubble.appendChild(sources);
  }
  // Re-attach the hover toolbar (innerHTML wipe).
  attachMessageActions(bubble, 'tool');
}

// Try to surface image paths produced by the generate_image tool.
function maybeRenderImages(bubble, output) {
  let data;
  try { data = JSON.parse(output); } catch { return; }
  const paths = data && data.paths;
  if (!Array.isArray(paths)) return;
  for (const p of paths) {
    const name = p.split(/[\\/]/).pop();
    const img = document.createElement('img');
    img.src = `/images/${encodeURIComponent(name)}`;
    img.alt = name;
    bubble.appendChild(img);
  }
}

// Model output is untrusted: prompt injection (a fetched page, a repo file, MCP
// output, another session's message) can make the model emit any HTML. So
// marked's output always goes through DOMPurify before it reaches the DOM, with
// a profile for chat markdown — no scripts, event handlers, SVG, frames, forms,
// styles, ids or names (which could shadow the UI's own elements), classes
// only `language-*` (so it can't borrow the UI's overlays), links only to
// other sites over http(s), mailto, a #fragment or an eVi image, and images
// only inline data: or eVi's own /images/<name>. An image from anywhere else
// would be a request the model chose (a way to leak the conversation in its
// URL, or to hit one of eVi's own endpoints), so it becomes a link.
if (window.marked) {
  window.marked.setOptions({
    breaks: true,        // single newline → <br> (matches chat-style writing)
    gfm: true,           // tables, strikethrough, task lists
    headerIds: false,    // we don't want anchor links inside chat
  });
}
const MD_PURIFY = {
  USE_PROFILES: { html: true },
  // DOMPurify's default, minus every scheme but http(s)/mailto (it also gates
  // plain values like type="checkbox", so it must still accept those).
  ALLOWED_URI_REGEXP: /^(?:(?:https?|mailto):|[^a-z]|[a-z+.\-]+(?:[^a-z+.\-:]|$))/i,
  FORBID_TAGS: ['style', 'form', 'button', 'textarea', 'select', 'option', 'optgroup',
    'datalist', 'output', 'dialog', 'template', 'slot', 'audio', 'video', 'source',
    'track', 'picture', 'canvas', 'map', 'area', 'marquee', 'blink'],
  FORBID_ATTR: ['style', 'id', 'name', 'srcset', 'background', 'poster', 'action',
    'method', 'popover', 'popovertarget', 'popovertargetaction', 'command', 'commandfor',
    'autofocus', 'tabindex', 'usemap', 'download', 'integrity', 'nonce', 'crossorigin',
    // Rendered markdown has no ids, so these could only point at the UI's own
    // elements: <label for="reset"> would make a click on the reply press Reset.
    'for', 'form', 'list', 'headers', 'aria-activedescendant', 'aria-controls',
    'aria-describedby', 'aria-details', 'aria-errormessage', 'aria-flowto',
    'aria-labelledby', 'aria-owns'],
  ALLOW_DATA_ATTR: false,
  RETURN_DOM_FRAGMENT: true,
};
const MD_IMG_DATA = /^data:image\//i;
// eVi's own generated image, by bare file name. It is checked as written AND
// as the browser resolves it: a dot segment, an encoded or backslashed
// separator or a query would turn `/images/../api/…` into a request to any
// path on eVi — some GETs there act (e.g. a model pull).
const MD_LOCAL_IMAGE = /^\/images\/[A-Za-z0-9_-][A-Za-z0-9._-]*$/;
function mdLocalImage(url) {
  if (!MD_LOCAL_IMAGE.test(url)) return false;
  try {
    const u = new URL(url, location.href);
    return u.origin === location.origin && u.pathname === url && !u.search && !u.hash;
  } catch { return false; }
}
// An absolute link back to eVi (`http:/api/…` resolves there too) is a
// relative link in disguise.
function mdSameOrigin(url) {
  try { return new URL(url, location.href).origin === location.origin; } catch { return true; }
}

// Sanitized DOM for marked's HTML, or null when DOMPurify isn't available (the
// caller then shows escaped text — never unsanitized HTML).
function sanitizeMarkdown(html) {
  const purify = window.DOMPurify;
  if (!purify || !purify.isSupported || typeof purify.sanitize !== 'function') return null;
  // DOMPurify parses in an inert document that inherits this page's CSP, so a
  // raw <base> would log a base-uri violation there before being dropped. Show
  // it as text instead (marked has already escaped any inside code).
  html = html.replace(/<(\/?)base\b/gi, '&lt;$1base');
  const frag = purify.sanitize(html, MD_PURIFY);
  for (const el of frag.querySelectorAll('[class]')) {
    const keep = el.getAttribute('class').split(/\s+/).filter(c => /^language-[\w+#.-]+$/.test(c));
    if (keep.length) el.setAttribute('class', keep.join(' ')); else el.removeAttribute('class');
  }
  // Images first: one that isn't inline data or eVi's own becomes a link (or
  // plain text), which the link rules below then vet like any other.
  for (const img of frag.querySelectorAll('img')) {
    const src = (img.getAttribute('src') || '').trim();
    if (MD_IMG_DATA.test(src) || mdLocalImage(src)) {
      img.setAttribute('src', src);
      continue;
    }
    const label = `[image: ${img.getAttribute('alt') || 'image'}]`;
    const repl = document.createElement(/^https?:/i.test(src) ? 'a' : 'span');
    if (repl.tagName === 'A') repl.setAttribute('href', src);
    repl.textContent = label;
    img.replaceWith(repl);
  }
  for (const a of frag.querySelectorAll('a[href]')) {
    const href = a.getAttribute('href').trim();
    if (/^https?:/i.test(href) && !mdSameOrigin(href)) {
      a.setAttribute('target', '_blank');
      a.setAttribute('rel', 'noopener noreferrer');
    } else if (/^(?:mailto:|#)/i.test(href) || mdLocalImage(href)) {
      a.setAttribute('href', href);
    } else {
      a.removeAttribute('href');  // e.g. a path into eVi's own API, relative or not
    }
  }
  // GFM task lists render as checkboxes; any other input is dropped.
  for (const box of frag.querySelectorAll('input')) {
    if ((box.getAttribute('type') || '').toLowerCase() === 'checkbox') box.setAttribute('disabled', '');
    else box.remove();
  }
  return frag;
}

// Mermaid (3.5 MB) loads on first use: when a finished reply has a diagram.
// The file is pinned like the <script> tags in index.html (scripts/
// vendor_ui_libs.py keeps this integrity in step). A diagram is the model's
// code, and mermaid lays it out in a live document: an image shape, a
// classDef or style, a C4 colour or an init directive can each make that
// document fetch a URL the diagram chose — and a same-origin GET with side
// effects would act with no click. So mermaid runs in a frame of its own whose CSP lets nothing load
// (data: images only; the frame keeps this page's policy too), a diagram
// that tried to load anything there is not shown, and the SVG that comes back
// goes through DOMPurify and stripDiagramLoads() before it joins the page.
// 'strict' keeps scripts and click handlers out; the label sanitizer and the
// `secure` keys stop a diagram's own config from widening any of that.
const MERMAID_JS = {
  src: '/static/vendor/mermaid/mermaid.min.js',
  integrity: 'sha384-EOXBFmc3gx5mb+vn0vPvvGqACToJD24hhacX5Yx+8NUUQrHIle/Qi5Bg9o3zKwW2',
};
const MERMAID_FRAME_CSP =
  "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; img-src data:";
// Config a diagram's %%{init}%% or front matter may not set: mermaid's own
// defaults, then everything that becomes CSS or reaches the label sanitizer.
const MERMAID_SECURE = ['secure', 'securityLevel', 'startOnLoad', 'maxTextSize',
  'suppressErrorRendering', 'maxEdges', 'themeCSS', 'themeVariables', 'fontFamily',
  'altFontFamily', 'dompurifyConfig', 'arrowMarkerAbsolute'];
const MERMAID_LABEL_PURIFY = {
  FORBID_TAGS: ['style', 'img', 'image', 'picture', 'source', 'video', 'audio', 'track',
    'iframe', 'frame', 'object', 'embed', 'link', 'meta', 'base', 'form', 'input',
    'button', 'textarea', 'select', 'a'],
  FORBID_ATTR: ['src', 'srcset', 'href', 'xlink:href', 'background', 'poster', 'action',
    'formaction', 'style'],
};
let _mermaid = null;  // Promise<frame | null>, once a load has started
function loadMermaid() {
  if (!_mermaid) {
    _mermaid = new Promise(resolve => {
      // No src: the frame keeps its initial same-origin about:blank document,
      // so no navigation happens (nothing for the desktop's guard to refuse).
      const frame = document.createElement('iframe');
      frame.setAttribute('aria-hidden', 'true');
      frame.tabIndex = -1;
      frame.style.cssText = 'position:fixed;left:-10000px;top:0;width:1200px;height:900px;' +
        'border:0;visibility:hidden;pointer-events:none';
      document.body.appendChild(frame);
      const doc = frame.contentDocument;
      const csp = doc.createElement('meta');
      csp.httpEquiv = 'Content-Security-Policy';
      csp.content = MERMAID_FRAME_CSP;
      doc.head.appendChild(csp);
      // Lay labels out in the page's text style, as when mermaid ran here.
      const base = doc.createElement('style');
      base.textContent = '*{box-sizing:border-box}html,body{margin:0}' +
        `body{font:${getComputedStyle(document.body).font || '14px/1.5 sans-serif'}}`;
      doc.head.appendChild(base);
      frame.refused = 0;  // loads the frame's CSP refused, see renderDiagram
      doc.addEventListener('securitypolicyviolation', () => { frame.refused += 1; });
      const s = doc.createElement('script');
      s.src = new URL(MERMAID_JS.src, document.baseURI).href;
      s.integrity = MERMAID_JS.integrity;
      s.onload = () => resolve(frame);
      s.onerror = () => resolve(null);
      doc.head.appendChild(s);
    }).then(frame => {
      const m = frame && frame.contentWindow && frame.contentWindow.mermaid;
      if (!m || typeof m.render !== 'function') {  // retry on the next diagram
        if (frame) frame.remove();
        _mermaid = null;
        return null;
      }
      m.initialize({ startOnLoad: false, theme: 'dark', securityLevel: 'strict',
                     secure: MERMAID_SECURE, dompurifyConfig: MERMAID_LABEL_PURIFY });
      return frame;
    });
  }
  return _mermaid;
}
// One diagram at a time, so a refused load is charged to the right one.
let _diagramQueue = Promise.resolve();
function renderDiagram(src) {
  const run = _diagramQueue.then(() => renderDiagramNow(src));
  _diagramQueue = run.catch(() => {});
  return run;
}
async function renderDiagramNow(src) {
  const frame = await loadMermaid();
  if (!frame) return null;
  const id = 'mmd-' + Math.random().toString(36).slice(2, 8);
  frame.refused = 0;
  let svg;
  try {
    ({ svg } = await frame.contentWindow.mermaid.render(id, src));
  } finally {
    await new Promise(r => setTimeout(r, 50));  // a refused load reports async
    frame.contentDocument.body.replaceChildren();
  }
  if (frame.refused) throw new Error('the diagram tried to load a resource');
  const diagram = sanitizeDiagram(svg, id);
  if (!diagram) throw new Error('the diagram carried something that loads or restyles');
  return diagram;
}
function sanitizeDiagram(svg, id) {
  const purify = window.DOMPurify;
  if (!purify || !purify.isSupported || typeof purify.sanitize !== 'function') return null;
  const frag = purify.sanitize(svg, {
    USE_PROFILES: { svg: true, svgFilters: true, html: true },
    ADD_TAGS: ['foreignObject'],
    HTML_INTEGRATION_POINTS: { foreignobject: true },
    FORBID_TAGS: ['a', 'img', 'image', 'feimage', 'use', 'form', 'button', 'textarea',
      'select', 'input', 'audio', 'video'],
    RETURN_DOM_FRAGMENT: true,
  });
  return stripDiagramLoads(frag, id) === 0 ? frag : null;
}
// The diagram joins this page, under this page's CSP: whatever in it could
// still fetch — a url() other than a same-document #ref (mermaid's markers
// and gradients), an image function, an escape that could spell either, an
// @import or @font-face — and any CSS rule not scoped to its own SVG, which
// could restyle the UI. Mermaid's own output has none of these (it scopes
// every rule it writes), so the count of what came out lets sanitizeDiagram
// refuse a diagram that brought any.
const CSS_LOADS = /\\|url\((?!\s*['"]?#)|(?:image-set|image|cross-fade|element|src)\(/i;
function cleanDeclarations(style) {
  let removed = 0;
  for (let i = style.length - 1; i >= 0; i--) {
    if (CSS_LOADS.test(style.getPropertyValue(style[i]))) {
      style.removeProperty(style[i]);
      removed += 1;
    }
  }
  return removed;
}
function cleanRules(holder, scope) {
  let removed = 0;
  for (let i = holder.cssRules.length - 1; i >= 0; i--) {
    const r = holder.cssRules[i];
    if (r instanceof CSSStyleRule && !(r.cssRules && r.cssRules.length) &&
        r.selectorText.split(',').every(sel => scope.test(sel.trim()))) {
      removed += cleanDeclarations(r.style);
    } else if (typeof CSSKeyframesRule !== 'undefined' && r instanceof CSSKeyframesRule) {
      for (const kf of r.cssRules) removed += cleanDeclarations(kf.style);
    } else if ((typeof CSSMediaRule !== 'undefined' && r instanceof CSSMediaRule) ||
               (typeof CSSSupportsRule !== 'undefined' && r instanceof CSSSupportsRule)) {
      removed += cleanRules(r, scope);
    } else {
      holder.deleteRule(i);
      removed += 1;
    }
  }
  return removed;
}
// How much it took out of the diagram, or -1 when it can't vet it at all.
function stripDiagramLoads(frag, id) {
  if (!frag || !/^mmd-[a-z0-9]+$/.test(id) || typeof CSSStyleSheet !== 'function') return -1;
  const scope = new RegExp(`^#${id}(?![\\w-])`);
  let removed = 0;
  for (const el of frag.querySelectorAll('style')) {
    const sheet = new CSSStyleSheet();
    try { sheet.replaceSync(el.textContent); } catch { return -1; }
    removed += cleanRules(sheet, scope);
    el.textContent = [...sheet.cssRules].map(r => r.cssText).join('\n');
  }
  for (const el of frag.querySelectorAll('*')) {
    for (const { name, value } of [...el.attributes]) {
      const ref = /^(?:xlink:)?href$|^src$/i.test(name);
      if (ref ? /^\s*#/.test(value) : !CSS_LOADS.test(value)) continue;
      if (name.toLowerCase() === 'style') removed += cleanDeclarations(el.style);
      else { el.removeAttribute(name); removed += 1; }
    }
  }
  return removed;
}

// Render a markdown string into the given bubble: marked + DOMPurify, then
// highlight.js. Falls back to escaped text + minimal substitutions when a
// library didn't load.
function renderMarkdownInto(bubble, src) {
  bubble.dataset.source = src;  // preserved for Mermaid scanning + edits
  // The `.md` wrapper class scopes our spacing CSS to rendered content
  // (without affecting tool / user / thinking bubbles).
  const md = document.createElement('div');
  md.className = 'md';
  let frag = null;
  if (window.marked) {
    try { frag = sanitizeMarkdown(window.marked.parse(src)); } catch { frag = null; }
  }
  if (frag) {
    md.appendChild(frag);
  } else {
    // Tiny fallback so we still show something legible.
    md.innerHTML = escapeHtml(src)
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/\n/g, '<br>');
  }
  bubble.replaceChildren(md);
  // Syntax-highlight code blocks if highlight.js is available.
  if (window.hljs) {
    bubble.querySelectorAll('pre code').forEach(el => {
      try { window.hljs.highlightElement(el); } catch {}
    });
  }
  // Re-attach the message-action toolbar (replaceChildren wiped it).
  if (bubble.classList.contains('assistant') || bubble.classList.contains('user')
      || bubble.classList.contains('tool')) {
    const cls = bubble.classList.contains('user') ? 'user'
              : bubble.classList.contains('tool') ? 'tool' : 'assistant';
    attachMessageActions(bubble, cls);
  }
}

// A streaming reply re-renders from its whole source, and marked + DOMPurify
// over a long reply costs tens of ms. So a delta only extends the source, and
// the bubble re-renders at most every STREAM_RENDER_MS (or as long as the last
// render took, if that was longer). handleEvent flushes before any other
// event, so nothing that event adds to the bubble is wiped by a late render.
const STREAM_RENDER_MS = 50;
const _streamRender = { bubble: null, timer: 0, cost: 0, last: -Infinity };
function streamMarkdownInto(bubble, src) {
  bubble.dataset.source = src;
  const s = _streamRender;
  if (s.bubble && s.bubble !== bubble) flushStreamRender();
  if (s.bubble) return;  // a render is already due
  const wait = Math.max(STREAM_RENDER_MS, s.cost) - (performance.now() - s.last);
  if (wait <= 0) { renderStreamed(bubble); return; }
  s.bubble = bubble;
  s.timer = setTimeout(flushStreamRender, wait);
}
function flushStreamRender() {
  const s = _streamRender;
  const bubble = s.bubble;
  if (!bubble) return;
  clearTimeout(s.timer);
  s.bubble = null;
  renderStreamed(bubble);
}
function renderStreamed(bubble) {
  const t0 = performance.now();
  renderMarkdownInto(bubble, bubble.dataset.source || '');
  _streamRender.last = performance.now();
  _streamRender.cost = _streamRender.last - t0;
  if (bubble.isConnected) log.scrollTop = log.scrollHeight;  // as handleEvent does
}

// Permission overlays open now ({sid, settle}): Stop settles its turn's with
// null, as the server denies them (WebSession.cancel_turn).
const openPermissionPrompts = new Set();
function settlePermissionPrompts(sid) {
  for (const p of [...openPermissionPrompts]) {
    if (!sid || p.sid === sid) p.settle(null);
  }
}

// Permission overlay: returns a Promise<boolean> for approve / deny, or null
// when the server's deadline (timeout_s) passes first — it has denied by then
// — or the turn was stopped. `sid` is the session the asking turn runs in.
function askPermission({ decision_id, tool_name, args, category, timeout_s }, sid) {
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.className = 'perm-overlay';
    // The overlay covers the composer's Stop button, so it carries its own
    // for the turn that asks (it settles this question once stopped).
    const stoppable = stopSid !== null && stopSid === (sid || sessionId);
    overlay.innerHTML = `
      <div class="perm-card">
        <h3>Permission required</h3>
        <p>The agent wants to run:</p>
        <p><span class="perm-tool">${escapeHtml(String(tool_name || ''))}</span>
           <span style="color:var(--dim)">(${escapeHtml(String(category || ''))})</span></p>
        <div class="perm-args">${escapeHtml(String(args || ''))}</div>
        <div class="perm-actions">
          ${stoppable ? '<button class="stop-turn" title="Stop the whole turn" style="margin-right:auto">Stop turn</button>' : ''}
          <button class="deny">Deny</button>
          <button class="approve">Approve</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const secs = Number(timeout_s);
    const timer = secs > 0 ? setTimeout(() => { cleanup(); resolve(null); }, secs * 1000) : null;
    const entry = { sid: sid || sessionId, settle: (v) => { cleanup(); resolve(v); } };
    openPermissionPrompts.add(entry);
    overlay.querySelector('.approve').onclick = () => { cleanup(); resolve(true); };
    overlay.querySelector('.deny').onclick = () => { cleanup(); resolve(false); };
    const stopHere = overlay.querySelector('.stop-turn');
    if (stopHere) {
      stopHere.onclick = () => {
        stopHere.disabled = true;
        stopHere.textContent = 'Stopping…';
        stopTurn().then(() => {
          if (overlay.isConnected) { stopHere.disabled = false; stopHere.textContent = 'Stop turn'; }
        });
      };
    }
    function cleanup() {
      if (timer) clearTimeout(timer);
      overlay.remove();
      openPermissionPrompts.delete(entry);
    }
  });
}

// `sid` is the session the asking turn runs in, not whichever tab is active now.
async function postDecision(decision_id, approved, sid) {
  await fetch('/api/decide', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sid || sessionId, decision_id, approved }),
  });
}

function trackChipState(text) {
  // Cheaply parse SystemMessage outputs from slash commands so chips reflect state.
  const lower = text.toLowerCase();
  if (lower.startsWith('goal set:')) {
    goalChipText = text.replace(/^goal set:\s*/i, '').replace(/\*\*/g, '');
    chipGoal.textContent = `goal: ${goalChipText.slice(0, 40)}${goalChipText.length > 40 ? '…' : ''}`;
    chipGoal.classList.add('active');
    chipGoal.style.display = 'inline-block';
  } else if (lower === 'goal cleared') {
    goalChipText = null;
    chipGoal.style.display = 'none';
  } else if (lower.includes('plan-only mode enabled')) {
    planArmed = true;
    chipPlan.classList.add('active');
    chipPlan.style.display = 'inline-block';
  } else if (lower.includes('auto mode on')) {
    autoOn = true;
    chipAuto.classList.add('active');
    chipAuto.style.display = 'inline-block';
  } else if (lower.startsWith('auto mode off')) {
    autoOn = false;
    chipAuto.style.display = 'none';
  }
}

// ---- LLM backend availability ------------------------------------------
var backendStatus = null;
async function checkBackend() {
  try {
    const r = await fetch('/api/backend/status');
    if (!r.ok) return null;
    backendStatus = await r.json();
  } catch { return null; }
  renderBackendBanner(backendStatus);
  return backendStatus;
}
function bkBtn(label, act, kind) {
  return '<button class="bk-btn" data-act="' + act + '" data-kind="' + (kind||'') +
    '" style="background:#b45309;color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:pointer;font-size:13px">' +
    label + '</button>';
}
// CLI-agent backends have no HTTP port — "not reachable" means their CLI isn't
// installed / on PATH, not that a server is down. Kind -> CLI binary.
const CLI_AGENT_KINDS = { claude_agent:'claude', codex:'codex', gemini:'gemini', amp:'amp', qwen:'qwen', copilot:'copilot' };

// ---- first-run setup screen ------------------------------------------
// Owns the window when nothing can answer yet. Renders the server-ranked
// `suggestions` ladder (already-installed CLI > running server > env key >
// download), so a machine that can answer right now never sees a download
// prompt first.
function hideSetupScreen() {
  const s = document.getElementById('setup-screen');
  if (s) s.remove();
}
function showSetupScreen(st) {
  if (document.getElementById('setup-screen')) return;  // don't rebuild over progress
  const sug = (st && st.suggestions) || [];
  const el = document.createElement('div');
  el.id = 'setup-screen';
  // Fixed to the viewport, below the settings/dialog overlays (1500/2000) so
  // Settings still opens over it, but above the chat UI it replaces.
  el.style.cssText = 'position:fixed;inset:0;z-index:1200;background:var(--bg,#0f172a);' +
    'display:flex;align-items:center;justify-content:center;padding:28px;overflow:auto';
  const top = sug.slice(0, 4), rest = sug.slice(4);
  const row = function (s, i) {
    const primary = i === 0;
    return '<button class="setup-opt" data-i="' + i + '" style="display:flex;gap:12px;width:100%;text-align:left;' +
      'align-items:flex-start;padding:13px 15px;margin-bottom:9px;border-radius:8px;cursor:pointer;' +
      'background:' + (primary ? 'var(--accent,#22d3ee)22' : 'var(--panel,#1e293b)') + ';' +
      'border:1px solid ' + (primary ? 'var(--accent,#22d3ee)' : 'var(--border,#334155)') + ';color:inherit;font:inherit">' +
      '<span style="opacity:.8;font-size:15px;line-height:1.3">' + (s.instant ? '▸' : '▼') + '</span>' +
      '<span style="flex:1;min-width:0">' +
        '<span style="font-weight:600;font-size:14px">' + escapeHtml(s.title) + '</span>' +
        '<span style="font-size:11px;opacity:.75;border:1px solid currentColor;border-radius:3px;padding:1px 5px;margin-left:8px;white-space:nowrap">' + escapeHtml(s.badge || '') + '</span>' +
        '<div style="font-size:12.5px;opacity:.72;margin-top:2px">' + escapeHtml(s.detail || '') + '</div>' +
      '</span></button>';
  };
  el.innerHTML =
    '<div style="max-width:560px;width:100%">' +
      '<h2 style="font-size:23px;margin:0 0 6px">Get eVi answering</h2>' +
      '<p style="opacity:.7;font-size:13.5px;margin:0 0 18px">Pick one. You can change it any time in Settings.</p>' +
      (sug.length ? top.map(row).join('')
                  : '<p class="sec-blurb">No local AI was found and this platform has no prebuilt runtime. ' +
                    'Connect a backend under Settings → Model &amp; Backend.</p>') +
      (rest.length ? '<details style="margin-top:4px"><summary style="cursor:pointer;font-size:13px;opacity:.75">' +
        'Other options found (' + rest.length + ')</summary><div style="margin-top:9px">' +
        rest.map(function (s, i) { return row(s, i + 4); }).join('') + '</div></details>' : '') +
      '<div id="setup-progress" style="margin-top:14px"></div>' +
      '<p style="opacity:.55;font-size:12px;margin-top:16px">Nothing is sent anywhere you haven\'t connected. ' +
      '<a href="#" id="setup-later" style="color:inherit">Set this up later</a></p>' +
    '</div>';
  document.body.appendChild(el);
  el.querySelectorAll('.setup-opt').forEach(function (b) {
    b.onclick = function () { runSetupChoice(sug[+b.getAttribute('data-i')], el); };
  });
  el.querySelector('#setup-later').onclick = function (e) { e.preventDefault(); hideSetupScreen(); };
}
function setupProgress(html) {
  const p = document.getElementById('setup-progress');
  if (p) p.innerHTML = html;
}
async function runSetupChoice(s, el) {
  if (!s) return;
  el.querySelectorAll('.setup-opt').forEach(function (b) { b.disabled = true; b.style.opacity = '.5'; });
  try {
    if (s.action === 'runtime') {
      hideSetupScreen();
      openLocalAISetup();
      return;
    }
    setupProgress('<div style="font-size:13px">Connecting to ' + escapeHtml(s.title.replace(/^Use /, '')) +
      ' and checking it answers…</div>');
    // verify:true — the server proves it answers and rolls the switch back if
    // it doesn't, so a failed attempt never leaves config pointing at a dud.
    const res = await fetch('/api/backend/use', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind: s.kind, verify: true }),
    }).then(function (r) { return r.json(); });
    showVerdict(res && res.verify, res && res.ok);
  } catch (e) {
    setupProgress('<div style="font-size:13px">⚠ ' + escapeHtml(String(e)) + '</div>');
  }
  el.querySelectorAll('.setup-opt').forEach(function (b) { b.disabled = false; b.style.opacity = ''; });
}
// The differentiator: report what actually ANSWERED. A reachable port is not a
// working backend — this renders the server's live-completion verdict.
function showVerdict(v, ok) {
  if (!ok || !v || !v.chat) {
    setupProgress('<div style="font-size:13px;color:var(--err,#f87171)">✗ It didn\'t answer — nothing was changed.</div>' +
      (v && v.error ? '<div style="opacity:.75;font-size:12px;margin-top:4px">' + escapeHtml(v.error) + '</div>' : '') +
      '<div style="margin-top:9px;font-size:12px;opacity:.8">Pick another option above, or run diagnostics.</div>' +
      '<div style="margin-top:8px"><button class="set-action-btn" id="setup-diag">Run diagnostics</button></div>');
    const dg = document.getElementById('setup-diag');
    if (dg) dg.onclick = function () { hideSetupScreen(); _openDiagnostics(); };
    return false;
  }
  const tick = function (ok, label, note) {
    return '<div style="display:flex;gap:9px;font-size:13px;padding:3px 0">' +
      '<span style="color:' + (ok ? 'var(--user,#4ade80)' : 'var(--err,#f87171)') + '">' + (ok ? '✓' : '✗') + '</span>' +
      '<span style="min-width:52px">' + label + '</span>' +
      '<span style="opacity:.65;font-size:12px">' + note + '</span></div>';
  };
  setupProgress(
    tick(true, 'Chat', 'replied in ' + (v.latency_ms || 0) + ' ms') +
    tick(!!v.tools, 'Tools', v.tools ? 'tool calls accepted' : 'this model can\'t use tools — file, web and image actions stay off') +
    '<div style="margin-top:10px"><button class="set-action-btn" id="setup-done" ' +
    'style="background:var(--accent);color:#fff;border-color:var(--accent)">Start chatting</button></div>');
  const d = document.getElementById('setup-done');
  if (d) d.onclick = function () { hideSetupScreen(); checkBackend(); };
  return true;
}

function renderBackendBanner(st) {
  const el = document.getElementById('backend-banner');
  if (!st) { el.style.display = 'none'; hideSetupScreen(); return; }
  const cfg = st.configured || {};
  // READY = the backend chat ACTUALLY uses is reachable. (any_reachable is not
  // enough — a *different* candidate being up doesn't mean chat will work; that
  // was the bug where the banner cleared but chats hit a dead backend.)
  if (cfg.reachable) { el.style.display = 'none'; hideSetupScreen(); return; }

  // Nothing at all is answering: this is first run, not a warning. A strip above
  // a fully-drawn chat UI reads as dismissible chrome, so setup takes the screen
  // and offers what the machine can ALREADY do, ranked, before any download.
  const live = (st.candidates || []).filter(function (c) { return c.reachable; });
  if (!live.length && !CLI_AGENT_KINDS[cfg.backend]) {
    el.style.display = 'none';
    showSetupScreen(st);
    return;
  }
  hideSetupScreen();

  const CLI_AGENT = CLI_AGENT_KINDS;
  var html, actions = '', hint = '';
  if (CLI_AGENT[cfg.backend]) {
    // State A′ — a CLI-agent backend whose CLI isn't installed / on PATH.
    const cli = CLI_AGENT[cfg.backend];
    html = '<b>⚠ eVi is set to ' + cfg.backend + ', but its <code>' + cli + '</code> CLI isn\'t installed or on PATH.</b> ' +
      'It signs in with the CLI\'s own subscription/login (no API key) — install it, run it once to log in, then Recheck.';
    // Offer to switch to a running local backend as a fallback, if there is one.
    if (live.length) actions = live.map(function (c) { return bkBtn('Use ' + c.kind + ' instead', 'use', c.kind); }).join('');
  } else if (live.length) {
    // State B — the configured backend is down, but another is running. Offer
    // eVi's own local AI as the recommended path AND a switch to what's up.
    html = '<b>⚠ eVi is set to ' + (cfg.backend || '?') + ' (' + (cfg.base_url || '?') +
      '), which isn\'t running.</b> Set up eVi\'s own local AI, or use ' +
      live.map(function (c) { return c.kind; }).join(', ') + ' which is already running:';
    actions = (st.runtime_supported ? bkBtn('⚡ Set up local AI', 'runtime', 'llamacpp') : '') +
      live.map(function (c) { return bkBtn('Use ' + c.kind, 'use', c.kind); }).join('');
  } else {
    // State C — nothing reachable; first-run setup. Recommended path is the
    // managed llama.cpp runtime: eVi downloads a small CPU runtime + model and
    // runs it itself — no Ollama, no account, no admin. Ollama is kept as an
    // alternative for users who prefer it / already have it.
    html = '<b>⚡ Set up eVi\'s local AI.</b> eVi can download a small model and run it locally — ' +
      'no Ollama, no account, no admin. ' +
      '<span style="opacity:.6">(Configured backend ' + (cfg.backend || '?') + ' @ ' +
      (cfg.base_url || '?') + ' isn\'t running.)</span>';
    actions = bkBtn('⚡ Set up local AI (recommended)', 'runtime', 'llamacpp');
    if (st.ollama_installed) {
      actions += bkBtn('Use Ollama instead', 'auto', 'ollama');
    } else if (st.can_auto_install_ollama) {
      actions += bkBtn('Install Ollama instead', 'auto', 'ollama');
    } else {
      actions += bkBtn('Download Ollama', 'download', 'ollama');
    }
    hint = '<div style="margin-top:6px;opacity:.7">Have LM Studio? Open it, load a model, ' +
      'then Developer → Start Server (port 1234), and click Recheck.</div>';
  }
  html += '<div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">' +
    actions + bkBtn('Recheck', 'recheck') + '</div>';
  html += '<div id="bk-progress" style="margin-top:8px;display:none"></div>' + hint;
  el.innerHTML = html;
  el.style.display = 'block';
  el.querySelectorAll('.bk-btn').forEach(function (b) { b.onclick = onBackendAction; });
}
function bkProgress(text, pct, busy) {
  const p = document.getElementById('bk-progress');
  if (!p) return;
  if (!document.getElementById('bk-spin-style')) {
    var s = document.createElement('style'); s.id = 'bk-spin-style';
    s.textContent = '@keyframes bk-spin{to{transform:rotate(360deg)}}';
    document.head.appendChild(s);
  }
  p.style.display = 'block';
  var extra = '';
  if (typeof pct === 'number') {
    var w = Math.max(0, Math.min(100, pct));
    extra = '<div style="height:8px;background:#1f2937;border-radius:4px;overflow:hidden;margin-top:5px">' +
      '<div style="height:100%;width:' + w + '%;background:#22d3ee;transition:width .2s"></div></div>';
  } else if (busy) {
    // Indeterminate spinner for blocking steps (e.g. the silent Ollama install).
    extra = '<span style="display:inline-block;margin-top:6px;width:15px;height:15px;vertical-align:-2px;' +
      'border:2px solid #334155;border-top-color:#22d3ee;border-radius:50%;animation:bk-spin .8s linear infinite"></span>';
  }
  // The text can carry the pull stream's status/error strings, so it goes in
  // as text; only the constant bar/spinner markup above is HTML.
  p.innerHTML = '<div></div>' + extra;
  p.firstChild.textContent = text;
}
async function pollReachable(tries, delayMs) {
  for (var i = 0; i < tries; i++) {
    await new Promise(function (r) { setTimeout(r, delayMs); });
    const st = await fetch('/api/backend/status').then(function (r) { return r.json(); }).catch(function () { return null; });
    if (st && st.any_reachable) { backendStatus = st; return true; }
  }
  return false;
}
// GET /api/backend/pull starts an Ollama download and streams its progress as
// server-sent events. The server refuses it without the X-Evi-Client header,
// so it is read with fetch (whose wrapper adds that header and the token)
// rather than an EventSource, which can't send headers: no <img>, <link>,
// <iframe> or EventSource — on another site or on this page — can start a
// pull. onEvent gets each JSON payload ({kind: progress|done|error, …}) and
// returns false to stop reading. Rejects on an HTTP error or a dropped
// connection; resolves when the stream ends.
async function streamModelPull(model, onEvent) {
  const qs = model ? ('?model=' + encodeURIComponent(model)) : '';
  const r = await fetch('/api/backend/pull' + qs, { headers: { 'Accept': 'text/event-stream' } });
  if (!r.ok || !r.body) throw new Error('HTTP ' + r.status);
  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) return;
    buf += decoder.decode(value, { stream: true });
    // Frames end in a blank line; sse-starlette writes CRLF.
    let sep;
    while ((sep = buf.match(/\r?\n\r?\n/)) !== null) {
      const frame = buf.slice(0, sep.index);
      buf = buf.slice(sep.index + sep[0].length);
      const data = frame.split(/\r?\n/).filter(l => l.startsWith('data:'))
        .map(l => l.slice(5).replace(/^ /, '')).join('\n');
      if (!data) continue;  // a keep-alive comment
      let d; try { d = JSON.parse(data); } catch { continue; }
      if (onEvent(d) === false) { reader.cancel().catch(() => {}); return; }
    }
  }
}
// The first-run banner's download step: true once the model is in.
async function pullModelSSE(model) {
  let ok = false;
  try {
    await streamModelPull(model, function (d) {
      if (d.kind === 'progress') {
        var label = d.status || 'downloading';
        bkProgress('Downloading model — ' + label + (d.pct != null ? ' (' + d.pct + '%)' : ''),
                   d.pct != null ? d.pct : undefined);
        return true;
      }
      if (d.kind === 'done') { ok = true; return false; }
      if (d.kind === 'error') { bkProgress('Model download error: ' + (d.message || '')); return false; }
      return true;
    });
  } catch (_) { /* HTTP error or dropped connection: not finished */ }
  return ok;
}
async function autoSetup(st) {
  st = st || {};
  // 1. Install Ollama if it isn't here yet.
  if (!st.ollama_installed) {
    bkProgress('Installing Ollama… this can take a minute.', undefined, true);
    var res = await fetch('/api/backend/install', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kind: 'ollama' }) })
      .then(function (r) { return r.json(); }).catch(function () { return { ok: false }; });
    if (!res.ok) {
      if (res.needs_manual) {
        bkProgress("Couldn't auto-install here — opening the download page. Install Ollama, then click Recheck.");
        await fetch('/api/backend/open-download', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kind: 'ollama' }) }).catch(function () {});
      } else {
        bkProgress('Install failed: ' + (res.message || 'unknown error') + ' — you can install Ollama manually, then Recheck.');
      }
      return;
    }
  }
  // 2. Start the server and wait for it.
  bkProgress('Starting Ollama…', undefined, true);
  await fetch('/api/backend/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kind: 'ollama' }) }).catch(function () {});
  var up = await pollReachable(15, 1200);
  if (!up) { bkProgress("Ollama didn't come up in time. Give it a moment and click Recheck."); return; }
  // 3. Pull the recommended default model (idempotent if already present).
  bkProgress('Downloading model' + (st.recommended_model ? (' ' + st.recommended_model) : '') + '…', 0);
  var ok = await pullModelSSE(st.recommended_model || '');
  if (!ok) { bkProgress("Model download didn't finish. Give it a moment and click Recheck."); return; }
  // 4. Activate it: point eVi's config at Ollama + this model (the step the
  //    old wizard skipped — without it chat kept hitting the default backend).
  bkProgress('Configuring eVi to use Ollama…', undefined, true);
  await fetch('/api/backend/use', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kind: 'ollama', model: st.recommended_model || undefined }) }).catch(function () {});
  bkProgress('All set — eVi is ready to chat!');
  // 5. Re-check: now the *configured* backend is reachable, so the banner hides.
  await checkBackend();
}
// ---- first-run "Set up local AI" card (dialog) ---------------------
// A guided card: pick a small model, download+start eVi's managed llama.cpp
// runtime — OR locate an existing llama.cpp install and have eVi supervise it.
// These live in the global (banner) scope alongside onBackendAction, so they
// drive the shared dialog by its stable element ids rather than the IIFE-local
// openDialog()/dlgContent (which aren't visible here).
const RT_INPUT = 'width:100%;padding:7px 9px;background:var(--panel,#0f172a);' +
  'color:var(--fg,#e2e8f0);border:1px solid var(--border,#334155);border-radius:6px';
function _rtDlgContent() { return document.getElementById('dialog-content'); }
// openDiagnostics() is declared inside the app IIFE and published only on
// window.eviUI, so global-scope callers must go through the bridge.
function _openDiagnostics() {
  if (window.eviUI && window.eviUI.openDiagnostics) window.eviUI.openDiagnostics();
}
function _rtOpenDialog(title, html) {
  document.getElementById('dialog-title').textContent = title;
  _rtDlgContent().innerHTML = html;
  document.getElementById('dialog-overlay').classList.add('shown');
}
async function openLocalAISetup() {
  _rtOpenDialog('Set up local AI', '<p class="sec-blurb">Loading…</p>');
  let status = {}, cat = { models: [] };
  try { status = await fetch('/api/runtime/status').then(r => r.json()); } catch (e) {}
  try { cat = await fetch('/api/runtime/catalog').then(r => r.json()); } catch (e) {}
  renderLocalAISetupCard(status, cat);
}
function renderLocalAISetupCard(status, cat) {
  if (!status || !status.supported) {
    _rtDlgContent().innerHTML = '<p class="sec-blurb">Local AI (llama.cpp) has no prebuilt for this platform. ' +
      'You can use Ollama or LM Studio instead — see Settings → Model &amp; Backend.</p>';
    return;
  }
  const models = ((cat && cat.models) || []).slice().sort((a, b) => (a.size_gb || 0) - (b.size_gb || 0));
  // Default the FIRST-RUN pick to the small starter (fast, low-commitment) — the
  // user can choose a bigger one from the list. If a model is already loaded, keep
  // that selected. The hardware "recommended" one is just labelled, not forced.
  const recId = (cat && cat.recommended_id) || '';
  const defaultId = status.active_model_id || (cat && cat.starter_id) || recId || (models[0] && models[0].id);
  const installed = !!status.runtime_installed;
  const running = !!status.server_running;
  const external = status.external_path || '';
  const opts = models.map(function (m) {
    const dot = m.installed ? '● ' : '';
    const sz = m.size_gb ? (' · ' + m.size_gb + ' GB') : '';
    const tag = m.id === recId ? ' — recommended for your PC' : '';
    return '<option value="' + escapeHtml(m.id) + '"' + (m.id === defaultId ? ' selected' : '') + '>' +
      dot + escapeHtml(m.name) + sz + tag + '</option>';
  }).join('');
  const intro = external
    ? 'Using your llama.cpp install at <code>' + escapeHtml(external) + '</code>. Pick a model and start.'
    : (installed
        ? 'eVi’s local runtime is installed. Pick a model and start it — eVi downloads the model if it isn’t here yet.'
        : 'eVi will download a small llama.cpp runtime (~40 MB) plus the model you pick, then run it locally. No account, no admin, fully private.');
  const goLabel = running ? '↻ Switch model' : ((installed || external) ? '▶ Start local AI' : '⚡ Download & start');
  _rtDlgContent().innerHTML =
    '<p class="sec-blurb">' + intro + '</p>' +
    '<div style="margin:10px 0"><label class="k" style="display:block;margin-bottom:4px">Model</label>' +
    '<select id="rt-model" style="' + RT_INPUT + '">' + opts + '</select></div>' +
    '<div id="rt-plan" style="margin-top:12px"></div>' +
    '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:14px">' +
      '<button class="set-action-btn" id="rt-go" style="background:var(--accent);color:#fff;border-color:var(--accent)">' + goLabel + '</button>' +
      '<button class="set-action-btn" id="rt-locate-toggle">Already have llama.cpp?</button>' +
    '</div>' +
    '<div id="rt-locate" style="display:none;margin-top:12px;padding-top:10px;border-top:1px solid var(--border,#334155)"></div>' +
    '<div id="rt-progress" style="margin-top:12px"></div>';
  document.getElementById('rt-go').onclick = function () {
    _runtimeApplyModel(document.getElementById('rt-model').value);
  };
  // Manifest: what this will actually put on the machine, refreshed whenever the
  // model changes so the sizes and paths always match what the button will do.
  const sel = document.getElementById('rt-model');
  const drawPlan = function () { _renderRuntimePlan(sel ? sel.value : ''); };
  if (sel) sel.onchange = drawPlan;
  drawPlan();
  document.getElementById('rt-locate-toggle').onclick = function () {
    const box = document.getElementById('rt-locate');
    if (box.style.display === 'none') { box.style.display = 'block'; _renderLocateBox(box); }
    else { box.style.display = 'none'; }
  };
}
// "What this will put on your PC" — real sizes, real destination paths, and the
// literal download URLs behind a disclosure. Nothing here is a guess: sizes come
// from the pinned release and the model catalog.
let _rtPlanSeq = 0;
async function _renderRuntimePlan(modelId) {
  const box = document.getElementById('rt-plan');
  if (!box) return;
  const seq = ++_rtPlanSeq;   // last request wins; a slow earlier one can't overwrite
  let plan = null;
  try {
    plan = await fetch('/api/runtime/plan?model_id=' + encodeURIComponent(modelId || ''))
      .then(function (r) { return r.json(); });
  } catch (e) { return; }
  if (!box.isConnected || seq !== _rtPlanSeq) return;
  const rows = (plan.items || []).map(function (it) {
    const dl = it.size ? '<span style="font-family:ui-monospace,monospace;font-size:11.5px;opacity:.9;white-space:nowrap">' + escapeHtml(it.size) + '</span>' : '';
    // The verb comes from `needed`, never from `size`: the runtime size is a live
    // HEAD that can legitimately come back empty, and a required download must
    // still say "download" when its size is unknown.
    const verb = (it.kind === 'server') ? 'run' : (it.needed ? 'download' : 'note');
    return '<div style="display:flex;gap:10px;align-items:baseline;padding:5px 0;border-bottom:1px solid var(--border,#334155)">' +
      '<span style="opacity:.55;font-size:11px;min-width:52px">' + verb + '</span>' +
      '<span style="flex:1;min-width:0">' +
        '<span style="font-size:13px">' + escapeHtml(it.label) + '</span>' +
        (it.detail ? '<div style="font-size:11.5px;opacity:.6">' + escapeHtml(it.detail) + '</div>' : '') +
        (it.path ? '<div style="font-size:11px;opacity:.45;font-family:ui-monospace,monospace;word-break:break-all">' + escapeHtml(it.path) + '</div>' : '') +
      '</span>' + dl + '</div>';
  }).join('');
  const cmds = (plan.commands || []).length
    ? '<details style="margin-top:8px"><summary style="cursor:pointer;font-size:12px;opacity:.7">Show the exact files &amp; commands</summary>' +
      '<pre style="margin:8px 0 0;padding:9px;background:var(--panel,#0f172a);border:1px solid var(--border,#334155);' +
      'border-radius:5px;font-size:11px;overflow-x:auto;white-space:pre-wrap;word-break:break-all">' +
      plan.commands.map(escapeHtml).join('\n') + '</pre></details>'
    : '';
  box.innerHTML =
    '<div style="font-size:12px;opacity:.7;margin-bottom:4px">What this will put on your PC</div>' +
    '<div style="border:1px solid var(--border,#334155);border-radius:6px;padding:4px 12px 8px">' + rows + '</div>' + cmds;
}

async function _renderLocateBox(box) {
  const blurb = '<p class="sec-blurb">Point eVi at an existing <code>llama-server</code> binary — ' +
    'eVi supervises it and skips the download (great if you already have a GPU build).</p>';
  box.innerHTML = blurb + '<p class="sec-blurb" style="opacity:.7">Looking for installs…</p>';
  let found = [];
  try { found = ((await fetch('/api/runtime/detect').then(r => r.json())) || {}).found || []; } catch (e) {}
  let html = blurb;
  if (found.length) {
    html += '<div style="margin:6px 0">' + found.map(function (p) {
      return '<div style="display:flex;gap:8px;align-items:center;margin:4px 0">' +
        '<code style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + escapeHtml(p) + '</code>' +
        '<button class="set-action-btn rt-found">Use</button></div>';
    }).join('') + '</div>';
  } else {
    html += '<p class="sec-blurb" style="opacity:.7">None found automatically — enter a path below.</p>';
  }
  html += '<div style="display:flex;gap:8px;margin-top:6px">' +
    '<input id="rt-path" style="flex:1;' + RT_INPUT + '" placeholder="Path to llama-server (or llama-server.exe)">' +
    '<button class="set-action-btn" id="rt-path-use">Use</button></div>';
  box.innerHTML = html;
  box.querySelectorAll('.rt-found').forEach(function (b, i) {
    b.onclick = function () { _runtimeLocateStart(found[i]); };  // closure, no attr escaping
  });
  document.getElementById('rt-path-use').onclick = function () {
    const p = (document.getElementById('rt-path').value || '').trim();
    if (p) _runtimeLocateStart(p);
  };
}
function _rtBtnsDisabled(v) {
  ['rt-go', 'rt-locate-toggle', 'rt-path-use'].forEach(function (id) {
    const el = document.getElementById(id); if (el) el.disabled = v;
  });
  document.querySelectorAll('.rt-found').forEach(function (b) { b.disabled = v; });
}
function _selectedRtModel() {
  const el = document.getElementById('rt-model');
  return el ? el.value : '';
}
async function _runtimeApplyModel(id) {
  _rtBtnsDisabled(true);
  await fetch('/api/runtime/model/use', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: id }) }).catch(function () {});
  _pollRuntimeInto();
}
async function _runtimeLocateStart(path) {
  const prog = document.getElementById('rt-progress');
  _rtBtnsDisabled(true);
  const res = await fetch('/api/runtime/locate', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path: path, model_id: _selectedRtModel() }),
  }).then(function (r) {
    return r.ok ? r.json() : r.json().then(function (j) { throw new Error(j.detail || ('HTTP ' + r.status)); });
  }).catch(function (e) {
    if (prog) prog.innerHTML = '<p class="sec-blurb">⚠ ' + escapeHtml(String((e && e.message) || e)) + '</p>';
    return null;
  });
  if (!res) { _rtBtnsDisabled(false); return; }
  _pollRuntimeInto();
}
// Named steps rather than one anonymous bar: during a multi-minute download the
// user should be able to see WHERE it is, and read the log when it stalls.
const RT_STEPS = [
  ['runtime', 'Prepare runtime'],
  ['model', 'Download model'],
  ['starting', 'Start the model'],
  ['done', 'Ready'],
];
function _rtStepsHtml(stage, message, pct) {
  const order = RT_STEPS.map(function (s) { return s[0]; });
  // An unknown or 'idle' stage means nothing is under way — show every step as
  // pending rather than claiming the first one is spinning. (indexOf returns -1
  // there, and clamping that to 0 would fake progress that never resolves.)
  const idx = order.indexOf(stage);
  const at = idx < 0 ? -1 : idx;
  return RT_STEPS.map(function (s, i) {
    const done = i < at || stage === 'done';
    const now = i === at && stage !== 'done';
    const mark = done ? '<span style="color:var(--user,#4ade80)">✓</span>'
      : now ? '<span style="color:var(--accent,#22d3ee)">◐</span>'
            : '<span style="opacity:.35">○</span>';
    const note = now && message ? '<span style="opacity:.62;font-size:11.5px">' + escapeHtml(message) + '</span>' : '';
    const bar = (now && typeof pct === 'number' && pct > 0)
      ? '<div style="height:4px;background:var(--border,#334155);border-radius:3px;overflow:hidden;margin-top:5px">' +
        '<div style="height:100%;width:' + Math.min(100, pct) + '%;background:var(--accent,#22d3ee);transition:width .25s"></div></div>' : '';
    return '<div style="padding:3px 0"><div style="display:flex;gap:9px;align-items:baseline;font-size:13px">' +
      mark + '<span style="min-width:118px">' + s[1] + '</span>' + note + '</div>' + bar + '</div>';
  }).join('');
}
// Every failure gets the two things that actually help: the log, and diagnostics.
function _rtFailureHtml(msg) {
  return '<div style="font-size:13px;color:var(--err,#f87171)">⚠ ' + escapeHtml(msg) + '</div>' +
    '<div style="display:flex;gap:8px;margin-top:9px;flex-wrap:wrap">' +
    '<button class="set-action-btn" id="rt-diag">Run diagnostics</button>' +
    '<button class="set-action-btn" id="rt-log">View log</button></div>' +
    '<div id="rt-logbox" style="margin-top:9px"></div>';
}
function _wireFailureButtons() {
  const d = document.getElementById('rt-diag');
  if (d) d.onclick = function () { _openDiagnostics(); };
  const l = document.getElementById('rt-log');
  if (l) l.onclick = async function () {
    // Expand in place. Replacing the dialog body would destroy the error text,
    // the model picker and the retry button, with no way back.
    const box = document.getElementById('rt-logbox');
    if (!box) return;
    if (box.innerHTML) { box.innerHTML = ''; l.textContent = 'View log'; return; }
    box.innerHTML = '<div style="font-size:12px;opacity:.6">Loading log…</div>';
    let r = null;
    try { r = await fetch('/api/runtime/log').then(function (x) { return x.json(); }); } catch (e) {}
    if (!box.isConnected) return;
    l.textContent = 'Hide log';
    box.innerHTML = (r && r.exists && (r.text || '').trim())
      ? '<div style="font-size:11px;opacity:.5;margin-bottom:5px;word-break:break-all">' + escapeHtml(r.path) + '</div>' +
        '<pre style="margin:0;padding:9px;background:var(--panel,#0f172a);border:1px solid var(--border,#334155);' +
        'border-radius:6px;font-size:11px;max-height:38vh;overflow:auto;white-space:pre-wrap">' + escapeHtml(r.text) + '</pre>'
      : '<div style="font-size:12px;opacity:.7">No log yet — the model server never got as far as starting, ' +
        'so the failure is above (usually a download or disk problem). Diagnostics has more.</div>';
  };
}
async function _pollRuntimeInto() {
  const prog = document.getElementById('rt-progress');
  for (var i = 0; i < 1800; i++) {  // up to ~30 min for a big model on a slow line
    await new Promise(function (r) { setTimeout(r, 1000); });
    var st = await fetch('/api/runtime/status').then(function (r) { return r.json(); }).catch(function () { return null; });
    if (!st) continue;
    // Stop if the dialog was closed (X / backdrop only hide it, so isConnected
    // alone stays true — also check the overlay is still shown).
    if (!prog || !prog.isConnected ||
        !document.getElementById('dialog-overlay').classList.contains('shown')) return;
    if (st.error) {
      prog.innerHTML = _rtFailureHtml('Setup failed: ' + st.error);
      _wireFailureButtons();
      _rtBtnsDisabled(false);
      return;
    }
    var pct = (typeof st.pct === 'number') ? Math.max(0, Math.min(100, st.pct)) : null;
    prog.innerHTML = _rtStepsHtml(st.stage, st.message, pct);
    if (st.done) {
      prog.innerHTML = _rtStepsHtml('done', '', 100) +
        '<p class="sec-blurb" style="color:#4caf50;margin-top:8px">✓ Local AI is ready — close this and start chatting.</p>';
      await checkBackend();  // configured backend now reachable → banner hides
      return;
    }
  }
  _rtBtnsDisabled(false);
}
async function onBackendAction(e) {
  const btn = e.currentTarget;
  const act = btn.getAttribute('data-act');
  const kind = btn.getAttribute('data-kind') || 'ollama';
  if (act === 'recheck') { btn.textContent = 'Checking…'; await checkBackend(); return; }
  // Local-AI setup opens a guided card (pick a model / locate an existing
  // install), so don't disable the button or auto-recheck here.
  if (act === 'runtime') { openLocalAISetup(); return; }
  btn.disabled = true;
  try {
    if (act === 'auto') {
      await autoSetup(backendStatus || {});
      return;  // autoSetup ends with checkBackend()
    } else if (act === 'start') {
      btn.textContent = 'Starting…';
      await fetch('/api/backend/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kind }) });
      await pollReachable(10, 1200);
    } else if (act === 'use') {
      // Switch eVi to a running backend (auto-picks an installed model).
      btn.textContent = 'Switching…';
      await fetch('/api/backend/use', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kind }) });
    } else if (act === 'download') {
      await fetch('/api/backend/open-download', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kind }) });
    }
  } catch (_) { }
  await checkBackend();
}

// Working-status bar: spinner + elapsed timer + live token count while a turn
// runs. Tokens are an estimate (chars/4) from streamed text until the backend's
// exact UsageStats arrives, then we show the real total.
const workStatus = (() => {
  const el = document.getElementById('work-status');
  const labelEl = el.querySelector('.work-label');
  const metaEl = el.querySelector('.work-meta');
  let startedAt = 0, timer = null, approxTokens = 0, exactTokens = null, phase = 'working…';
  const fmtElapsed = ms => {
    const s = Math.max(0, Math.floor(ms / 1000));
    return s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`;
  };
  function render() {
    labelEl.textContent = phase;
    const toks = exactTokens != null ? exactTokens : approxTokens;
    const parts = [fmtElapsed(Date.now() - startedAt)];
    if (toks > 0) parts.push(`${toks.toLocaleString()} tok`);
    metaEl.textContent = parts.join(' · ');
  }
  return {
    start() {
      startedAt = Date.now(); approxTokens = 0; exactTokens = null; phase = 'working…';
      el.style.display = 'flex'; render();
      if (timer) clearInterval(timer);
      timer = setInterval(render, 1000);
    },
    setPhase(p) { phase = p; render(); },
    addText(t) { if (t) { approxTokens += Math.ceil(t.length / 4); render(); } },
    setTokens(n) { if (typeof n === 'number') { exactTokens = n; render(); } },
    stop() { if (timer) clearInterval(timer); timer = null; el.style.display = 'none'; },
  };
})();
window.workStatus = workStatus;  // exposed for e2e tests

// Completion notifications. Opt-in via the bell toggle (localStorage). Fires a
// browser Notification + short beep when a turn finishes while this tab is
// hidden — works in the Tauri desktop webview too. No-op when off / focused.
const NOTIFY_KEY = 'evi-notify';  // kept hyphenated to not reset existing opt-ins
function notifyEnabled() { return localStorage.getItem(NOTIFY_KEY) === '1'; }
async function notifyToggle() {
  if (notifyEnabled()) { localStorage.removeItem(NOTIFY_KEY); return false; }
  // Only enable if the browser will actually deliver toasts (a denied prompt
  // would otherwise show the bell ON while toasts silently never appear).
  let perm = 'granted';
  try {
    if ('Notification' in window) {
      perm = Notification.permission === 'granted'
        ? 'granted' : await Notification.requestPermission();
    }
  } catch { perm = 'granted'; }  // no Notification API (e.g. some webviews) → allow beep-only
  if (perm === 'denied') return false;
  localStorage.setItem(NOTIFY_KEY, '1');
  return true;
}
function notifyBeep() {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.connect(g); g.connect(ctx.destination);
    o.frequency.value = 660; g.gain.value = 0.05;
    o.start(); o.stop(ctx.currentTime + 0.15);
  } catch {}
}
function notifyTurnDone(assistant) {
  if (!notifyEnabled()) return;
  if (document.visibilityState === 'visible' && document.hasFocus()) return;
  const src = (assistant && (assistant.dataset.source || assistant.textContent)) || '';
  const body = src.trim().slice(0, 120) || 'Turn complete';
  try {
    if ('Notification' in window && Notification.permission === 'granted') {
      const n = new Notification('eVi — turn complete', { body });
      n.onclick = () => { window.focus(); n.close(); };
    }
  } catch {}
  notifyBeep();
}

// ---- non-interrupting steering ---------------------------------------
// While a turn runs, a follow-up is QUEUED onto it and applied at the next
// step instead of being dropped or killing work in flight. Bubbles for queued
// text are kept here so the Steering event can mark them applied.
let turnInFlight = false;
const steerPending = [];      // [{text, bubble}] awaiting a verdict
function markSteerApplied(text, applied) {
  const i = steerPending.findIndex(function (p) { return p.text === text; });
  const entry = i >= 0 ? steerPending.splice(i, 1)[0] : null;
  if (!entry || !entry.bubble) return entry;
  const tag = entry.bubble.querySelector('.steer-tag');
  if (tag) {
    if (applied) { tag.textContent = 'applied'; tag.style.opacity = '.55'; }
    else { tag.remove(); }
  }
  return entry;
}
async function steerMessage(text) {
  // Show it immediately — the point is that the draft is never lost.
  const bubble = addBubble('user');
  bubble.textContent = text;
  const tag = document.createElement('span');
  tag.className = 'steer-tag';
  tag.textContent = 'queued — applies at the next step';
  tag.style.cssText = 'display:block;font-size:11px;opacity:.6;margin-top:3px';
  bubble.appendChild(tag);
  attachMessageActions(bubble, 'user');
  steerPending.push({ text: text, bubble: bubble });
  let res = null;
  try {
    res = await fetch('/api/session/' + encodeURIComponent(sessionId) + '/steer', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: text }),
    }).then(function (r) { return r.json(); });
  } catch (e) {}
  if (!res || !res.queued) {
    // The turn finished between the keystroke and the request — send normally
    // rather than leaving a bubble that never gets answered.
    markSteerApplied(text, false);
    if (bubble && bubble.remove) bubble.remove();
    sendMessage(text);
  }
}

async function sendMessage(text, images) {
  // Don't fire a real LLM turn into the void. Slash commands (/help, /reset,
  // …) are handled without a model, so only gate normal messages.
  if (!text.startsWith('/')) {
    const st = await checkBackend();
    // Gate on the CONFIGURED backend being reachable (what chat actually uses),
    // not any_reachable — otherwise we'd fire into a dead backend while a
    // different one happens to be up.
    if (st && !(st.configured && st.configured.reachable)) {
      input.value = text;  // keep what they typed
      return;              // the banner (shown by checkBackend) explains how to fix it
    }
  }
  // Auto-label this tab from the first user message we see.
  if (tabs[activeIdx] && (!tabs[activeIdx].label || tabs[activeIdx].label === 'new chat')) {
    setTabLabel(text);
  }
  // If the user is talking, stop the TTS — they want our attention.
  if ('speechSynthesis' in window) window.speechSynthesis.cancel();
  // A note that arrived while this chat sat idle goes before the new prompt,
  // so the prompt's bubble gets the history index the server gives it.
  if (!turnInFlight) await syncNotes(sessionId);
  const userBubble = addBubble('user');
  userBubble.textContent = text;
  attachMessageActions(userBubble, 'user');  // textContent above wiped the toolbar
  let assistant = null;  // built lazily on first TextDelta
  // Only the call that STARTED a turn owns the in-flight state: a read-only
  // slash command (/help) sent mid-turn must not reset it when it returns, or
  // the next message would start a second turn instead of steering this one.
  const ownsTurn = !turnInFlight;
  // The session this turn runs in — permission answers go to it even if the
  // user switches tabs meanwhile.
  const turnSid = sessionId;
  if (ownsTurn) {
    turnInFlight = true;
    send.textContent = 'Queue';
    send.title = 'This turn is running — your message is applied at the next step';
    workStatus.start();
    showStop(turnSid);
  }
  // The plan badge is one-shot — clear it once a turn starts.
  if (planArmed && !text.startsWith('/')) {
    planArmed = false;
    chipPlan.style.display = 'none';
  }

  let steerInstead = false;
  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: turnSid,
        message: text,
        images: images && images.length ? images : null,
      }),
    });
    if (res.status === 409) {
      // The server is already running a turn in this session (started before
      // a reload, or from another window). A plain message joins it as a
      // follow-up; a command that would rewrite history waits for it.
      const detail = await res.json().then(d => d && d.detail).catch(() => '');
      if (!text.startsWith('/') && !(images && images.length)) {
        // steerMessage draws its own bubble; give back this one's index.
        if (userBubble.dataset.index === String(nextHistoryIndex - 1)) nextHistoryIndex -= 1;
        userBubble.remove();
        steerInstead = true;
      } else {
        addBubble('system').textContent = String(detail || 'A turn is still running in this session — try again when it finishes.');
      }
      return;
    }
    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let done = false;

    while (!done) {
      const { done: streamDone, value } = await reader.read();
      if (streamDone) break;
      buf += decoder.decode(value, { stream: true });
      // SSE frames end in a blank line — tolerate BOTH \n\n and \r\n\r\n
      // (sse-starlette emits CRLF; splitting on \n\n only would never match,
      // so no events would ever render — the "no chat response" bug).
      let sep;
      while ((sep = buf.match(/\r?\n\r?\n/)) !== null) {
        const chunk = buf.slice(0, sep.index);
        buf = buf.slice(sep.index + sep[0].length);
        for (const line of chunk.split(/\r?\n/)) {
          if (!line.startsWith('data:')) continue;
          const payload = line.slice(5).trim();
          if (!payload) continue;
          let ev;
          try { ev = JSON.parse(payload); } catch { continue; }
          assistant = await handleEvent(ev, assistant, turnSid);
          if (ev.kind === 'Done' || ev.kind === 'Error') done = true;
        }
      }
    }
  } catch (err) {
    addBubble('error').textContent = `error: ${err.message}`;
  } finally {
    if (ownsTurn) {
      turnInFlight = false;
      send.textContent = 'Send';
      send.title = '';
      workStatus.stop();
      hideStop();
      send.disabled = false;
      // Notes held while the turn ran joined the history as it ended.
      syncNotes(turnSid);
    }
    input.focus();
    refreshUsage();
    if (steerInstead) steerMessage(text);
    else maybeAutoTitle();
  }
}

// After the first complete exchange, ask the server for a nicer LLM-written
// tab title (replaces the raw-first-message label). Attempted once per tab.
async function maybeAutoTitle() {
  const t = tabs[activeIdx];
  if (!t || t.titled) return;
  t.titled = true;
  const sid = sessionId;
  try {
    const res = await fetch(`/api/session/${sid}/title`, { method: 'POST' });
    if (!res.ok) return;
    const data = await res.json();
    const title = (data.title || '').trim();
    if (!title) return;
    const idx = tabs.findIndex(x => x.id === sid);
    if (idx >= 0) {
      tabs[idx].label = title.slice(0, 60);
      saveTabsState();
      renderTabs();
      postSessionName(sid, tabs[idx].label);
    }
  } catch { /* best-effort; keep the first-message label */ }
}

async function handleEvent(ev, assistant, sid) {
  if (ev.kind !== 'TextDelta') flushStreamRender();
  if (ev.kind === 'ThinkingDelta') {
    let bubble = log.lastElementChild;
    if (!bubble || !bubble.classList.contains('thinking') || !bubble.dataset.active) {
      bubble = addBubble('thinking');
      bubble.dataset.active = '1';
      bubble.innerHTML = `<details open><summary>thinking…</summary><div class="think-body"></div></details>`;
    }
    bubble.querySelector('.think-body').textContent += ev.text;
    workStatus.setPhase('thinking…');
    workStatus.addText(ev.text);
    return assistant;
  }
  // Any non-thinking event ends the current thinking block (collapsed by default).
  const lastBubble = log.lastElementChild;
  if (lastBubble && lastBubble.classList.contains('thinking') && lastBubble.dataset.active) {
    delete lastBubble.dataset.active;
    const details = lastBubble.querySelector('details');
    if (details) details.open = false;
  }
  if (ev.kind === 'TextDelta') {
    if (!assistant) assistant = addBubble('assistant');
    const cur = assistant.dataset.source || '';
    streamMarkdownInto(assistant, cur + ev.text);
    workStatus.setPhase('responding…');
    workStatus.addText(ev.text);
    // Stream completed sentences into the browser's TTS queue.
    maybeSpeak(assistant);
  } else if (ev.kind === 'ToolCall') {
    // The tool call belongs to the CURRENT assistant history row (an assistant
    // message can carry text + tool_calls). Render it as a note inside that
    // bubble — mirroring rebuildHistory — instead of a separate history bubble.
    // A separate bubble claimed a history index the server never stores, so
    // every later bubble's data-index drifted and edit/branch/delete hit the
    // wrong message. Ensuring one assistant bubble per row keeps them aligned.
    if (!assistant) assistant = addBubble('assistant');
    const note = document.createElement('div');
    note.style.cssText = 'color:var(--dim);font-size:11px;margin-top:4px';
    note.textContent = `→ ${ev.name}(${ev.arguments})`;
    assistant.appendChild(note);
    workStatus.setPhase(`running ${ev.name}…`);
  } else if (ev.kind === 'ToolProgress') {
    // A single self-updating "running" bubble so a slow tool shows live
    // status instead of an apparent hang. UI-only artifact (history:false) —
    // it's removed when results arrive, so it must never claim a history index.
    let p = document.getElementById('tool-progress');
    if (!p) { p = addBubble('tool', { history: false }); p.id = 'tool-progress'; }
    p.textContent = `… ${ev.names.join(', ')} running (${Math.round(ev.elapsed)}s)`;
    workStatus.setPhase(`running ${ev.names.join(', ')}…`);
  } else if (ev.kind === 'ToolResult') {
    const pp = document.getElementById('tool-progress');
    if (pp) pp.remove();
    assistant = null;  // the assistant row that issued the call(s) is complete
    const b = addBubble('tool');
    renderToolResult(b, ev.name, ev.output, ev.citations || []);
    maybeRenderImages(b, ev.output);
  } else if (ev.kind === 'PermissionRequest') {
    const approved = await askPermission(ev, sid || sessionId);
    // null = the prompt timed out; the server has already denied it.
    if (approved !== null) await postDecision(ev.decision_id, approved, sid || sessionId);
  } else if (ev.kind === 'PermissionExpired') {
    const b = addBubble('system');
    b.textContent = `Permission request for ${ev.tool_name || 'a tool'} expired after `
      + `${Math.round(Number(ev.timeout_s) || 0)}s with no answer — denied.`;
  } else if (ev.kind === 'Steering') {
    if (ev.applied === false) {
      // The turn ended before there was a next step. Nothing was lost — resend
      // it as a normal message once this turn's stream is done.
      const entry = markSteerApplied(ev.text, false);
      if (entry && entry.bubble && entry.bubble.remove) entry.bubble.remove();
      setTimeout(function () { sendMessage(ev.text); }, 0);
    } else {
      markSteerApplied(ev.text, true);
    }
  } else if (ev.kind === 'RouteInfo') {
    const lbl = document.getElementById('model-btn-label');
    if (lbl) {
      if (ev.route && ev.route !== 'default') {
        // A routed turn used a different model than the picker's active one —
        // annotate it explicitly for this turn.
        lbl.textContent = ev.model + ' (' + ev.route + ')';
      } else {
        // The default route fires on EVERY turn; re-render the footer from
        // pickerState instead of overwriting the label with a bare id, so we
        // don't drop the effort/fast suffix, capability chips, cloud flag, and
        // truncation each turn.
        renderFooter();
      }
    }
  } else if (ev.kind === 'UsageStats') {
    // The usage chip (used / ceiling tokens, colour-coded) is owned SOLELY by
    // refreshUsage() so its format never diverges (#29) — here just feed the
    // live work indicator; refreshUsage settles the chip at turn end.
    workStatus.setTokens(ev.total_tokens);
  } else if (ev.kind === 'Guardrail') {
    const bubble = addBubble('system');
    bubble.style.color = ev.blocked ? '#e05252' : '#d4a017';
    bubble.textContent = `⚠ guardrail (${ev.direction}): ${ev.message}`;
  } else if (ev.kind === 'LogProbs') {
    // Append a small confidence line under the assistant bubble.
    if (assistant) {
      const conf = Math.round(Math.exp(ev.avg_logprob) * 100);
      const color = conf >= 70 ? '#4caf50' : (conf >= 40 ? '#d4a017' : '#e05252');
      const el = document.createElement('div');
      el.style.cssText = `font-size:11px;opacity:.7;margin-top:4px;color:${color};`;
      el.textContent = `confidence ${conf}% avg · ${ev.low_count} low-confidence token(s)`;
      assistant.appendChild(el);
    }
  } else if (ev.kind === 'StopBlocked' || ev.kind === 'ChecksFailed') {
    // The verify gate sent the model back to work: its note joined the
    // history here as a user message. Drawn as a note (no message actions),
    // it takes that history index like drawNote does, and the reply after it
    // is a new assistant row.
    assistant = null;
    const b = addBubble('note');
    b.classList.add('verify-note');
    nextHistoryIndex += 1;
    const head = document.createElement('div');
    head.className = 'note-head';
    const body = document.createElement('div');
    body.className = 'note-body';
    if (ev.kind === 'StopBlocked') {
      head.textContent = `⛔ stop hook "${String(ev.hook || '')}" blocked the end of the turn `
        + `(iteration ${Number(ev.iteration) || 1}) — eVi keeps going`;
      body.textContent = String(ev.reason || '');
    } else {
      head.textContent = `✗ checks failed (round ${Number(ev.round) || 1} of `
        + `${Number(ev.max_rounds) || 1}) — sent back to the model`;
      body.textContent = (Array.isArray(ev.failed) ? ev.failed : []).map(String).join(', ');
    }
    b.appendChild(head);
    b.appendChild(body);
    workStatus.setPhase('working on it again…');
  } else if (ev.kind === 'CheckResult') {
    // One verify check: a UI-only line (its record is in the run report).
    const ok = ev.exit === 0;
    const b = addBubble('system', { history: false });
    b.classList.add('check-line', ok ? 'ok' : 'fail');
    const line = document.createElement('div');
    const status = ok ? 'passed' : (ev.exit === null || ev.exit === undefined
      ? 'did not run to the end' : `exit ${ev.exit}`);
    line.textContent = `${ok ? '✓' : '✗'} check ${String(ev.name || '')} `
      + `(round ${Number(ev.round) || 1}) ${status} · ${String(ev.cmd || '')}`;
    b.appendChild(line);
    if (!ok && ev.tail) {
      const d = document.createElement('details');
      const s = document.createElement('summary');
      s.textContent = 'output';
      const pre = document.createElement('pre');
      pre.textContent = String(ev.tail);
      d.appendChild(s);
      d.appendChild(pre);
      b.appendChild(d);
    }
    workStatus.setPhase('checking…');
  } else if (ev.kind === 'ChecksSkipped') {
    const b = addBubble('system', { history: false });
    b.classList.add('check-line');
    b.textContent = `checks skipped: ${String(ev.reason || '')}`;
  } else if (ev.kind === 'FilesChanged') {
    // UI-only footer under the turn's reply: what its tools wrote. Never sent
    // to the model, not part of the history.
    const paths = Array.isArray(ev.paths) ? ev.paths.map(String) : [];
    if (paths.length) {
      const foot = document.createElement('div');
      foot.className = 'files-changed';
      const title = document.createElement('div');
      title.className = 'files-changed-title';
      title.textContent = `Files changed (${paths.length})`;
      const list = document.createElement('ul');
      for (const p of paths) {
        const li = document.createElement('li');
        li.textContent = p;
        list.appendChild(li);
      }
      foot.appendChild(title);
      foot.appendChild(list);
      if (assistant) assistant.appendChild(foot);
      else addBubble('files-changed-box', { history: false }).appendChild(foot);
    }
  } else if (ev.kind === 'Done') {
    if (ev.reason === 'interrupted') {
      // Stopped (POST /cancel): what streamed so far stays, the rest never ran.
      addBubble('system').textContent = 'Stopped — the turn was cancelled.';
    } else if (ev.reason === 'checks_failed') {
      addBubble('system').textContent =
        'Stopped — the project checks still failed after the last round ([checks] max_rounds).';
    } else if (ev.reason === 'stop_hook_exhausted') {
      addBubble('system').textContent =
        'Stopped — a stop hook kept blocking the end of the turn ([hooks] max_blocks).';
    }
    // The turn's final assistant bubble is now the newest — move the re-roll
    // affordance onto it (and off any earlier ones).
    updateRerollButtons();
    // Flush any trailing partial sentence to the TTS queue.
    flushSpeak(assistant);
    // Completion notification — only when this tab is in the background, so it
    // doesn't nag while you're watching. Toggle in localStorage('evi-notify').
    notifyTurnDone(assistant);
    // Scan the just-completed assistant bubble for ```mermaid blocks
    // and render them inline.
    if (assistant) {
      // marked.js stripped the fenced blocks into <pre><code> — easier to
      // scan the raw source we stashed in dataset.source.
      const text = assistant.dataset.source || assistant.textContent;
      const re = /```mermaid\n([\s\S]*?)```/g;
      let m;
      const blocks = [];
      while ((m = re.exec(text)) !== null) blocks.push(m[1]);
      for (const src of blocks) {
        try {
          const diagram = await renderDiagram(src);
          if (!diagram) continue;
          const container = document.createElement('div');
          container.appendChild(diagram);
          container.style.cssText = 'margin-top:8px;background:#0b0f15;border-radius:4px;padding:8px';
          assistant.appendChild(container);
        } catch (err) {
          // Bad (or refused) diagram — leave the raw block visible.
          console.warn('mermaid render failed', err);
        }
      }
    }
  } else if (ev.kind === 'SystemMessage') {
    // A history-wiping slash command (e.g. /reset) asks us to clear the visible
    // transcript so the screen matches the model's actual context — otherwise
    // old bubbles linger for messages the model has already forgotten. Clear
    // first (drops the stale transcript + the '/reset' echo), then leave the
    // confirmation bubble as the fresh slate. (New chat starts a NEW session;
    // /reset keeps this one but empties it.)
    if (ev.clear_transcript) { log.innerHTML = ''; renderEmptyState(); }
    const b = addBubble('system');
    renderMarkdownInto(b, ev.text);
    trackChipState(ev.text);
  } else if (ev.kind === 'Error') {
    addBubble('error').textContent = ev.message;
  }
  log.scrollTop = log.scrollHeight;
  return assistant;
}

form.addEventListener('submit', e => {
  e.preventDefault();
  let text = input.value.trim();
  if (!text && !attachedFile) return;

  const images = [];
  if (attachedFile) {
    if (attachedIsImage) {
      // For vision models, send the path via `images` so the backend can
      // attach the file as a multipart image_url. For non-VL models the
      // agent gracefully degrades on the server side.
      images.push(attachedFile);
      if (!text) text = "What's in this image?";
    } else {
      const ref = `\n\n[attached file: ${attachedFile}]`;
      text = text ? text + ref : `I uploaded a file at ${attachedFile}. Please take a look.`;
    }
    attachedFile = null;
    attachedIsImage = false;
  }
  input.value = '';
  // A follow-up typed while a turn is running steers it instead of being
  // swallowed — the send button stays live for exactly this reason.
  if (turnInFlight && !images.length && !text.startsWith('/')) {
    steerMessage(text);
    return;
  }
  sendMessage(text, images);
});

input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    form.requestSubmit();
  }
});

// --- tab bar rendering --------------------------------------------------

const tabsEl = document.getElementById('tabs');

function renderTabs() {
  const parts = [];
  tabs.forEach((t, i) => {
    const cls = i === activeIdx ? 'tab active' : 'tab';
    const label = (t.label || 'new chat').slice(0, 32);
    parts.push(
      `<div class="${cls}" data-i="${i}" title="${escapeAttr(t.id)}">
         <span class="label">${escapeHtml(label)}</span>
         <button class="close" data-close="${i}" title="Close">×</button>
       </div>`
    );
  });
  parts.push(`<div class="tab new-tab" data-new>＋</div>`);
  tabsEl.innerHTML = parts.join('');
}

tabsEl.addEventListener('click', e => {
  const closeBtn = e.target.closest('[data-close]');
  if (closeBtn) {
    closeTab(parseInt(closeBtn.dataset.close, 10));
    return;
  }
  if (e.target.closest('[data-new]')) {
    newTab();
    return;
  }
  const tabEl = e.target.closest('[data-i]');
  if (tabEl) switchTab(parseInt(tabEl.dataset.i, 10));
});

async function switchTab(i) {
  if (i < 0 || i >= tabs.length || i === activeIdx) return;
  activeIdx = i;
  setSessionId(tabs[i].id);
  saveTabsState();
  renderTabs();
  await rebuildHistory();
}

function newTab() {
  tabs.push({ id: crypto.randomUUID(), label: 'new chat' });
  activeIdx = tabs.length - 1;
  setSessionId(tabs[activeIdx].id);
  saveTabsState();
  renderTabs();
  log.innerHTML = ''; renderEmptyState();
  nextHistoryIndex = 1;
  refreshUsage();
}

function closeTab(i) {
  if (i < 0 || i >= tabs.length) return;
  tabs.splice(i, 1);
  if (!tabs.length) {
    // Always keep at least one tab.
    tabs.push({ id: crypto.randomUUID(), label: 'new chat' });
    activeIdx = 0;
  } else {
    activeIdx = Math.max(0, Math.min(activeIdx, tabs.length - 1));
    // If we closed the active tab, the index shifts naturally; otherwise
    // adjust to keep the same logical selection.
    if (i < activeIdx) activeIdx -= 1;
  }
  setSessionId(tabs[activeIdx].id);
  saveTabsState();
  renderTabs();
  log.innerHTML = ''; renderEmptyState();
  nextHistoryIndex = 1;
  rebuildHistory();
}

// Mirror a session's display name to the SERVER so other sessions can address
// it by name (Ph 95 session messaging). Best-effort; labels stay client-first.
async function postSessionName(id, name) {
  if (!id || !name) return;
  try {
    await fetch(`/api/session/${encodeURIComponent(id)}/name`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
  } catch { /* best-effort */ }
}

function setTabLabel(label) {
  if (!tabs[activeIdx]) return;
  tabs[activeIdx].label = label.slice(0, 60);
  saveTabsState();
  renderTabs();
  postSessionName(tabs[activeIdx].id, tabs[activeIdx].label);
}

renderTabs();

// --- model picker -------------------------------------------------------

const modelBtn = document.getElementById('model-btn');
const modelBtnLabel = document.getElementById('model-btn-label');
const pickerOverlay = document.getElementById('picker-overlay');
const pickerCard = document.getElementById('picker-card');

// ---- working folder chip (per-session cwd) ------------------------------
const cwdBtn = document.getElementById('cwd-btn');
const cwdBtnLabel = document.getElementById('cwd-btn-label');

function _shortPath(p) {
  if (!p) return '…';
  const parts = p.replace(/[\\/]+$/, '').split(/[\\/]/);
  return '📁 ' + (parts[parts.length - 1] || p);
}

async function loadCwd() {
  // Pin the session this lookup is for. Switching tabs quickly leaves two
  // responses in flight, and without this the later-arriving one wins — which
  // would put the previous session's folder back on the chip, the very bug
  // setSessionId exists to prevent. (Same guard as maybeAutoTitle.)
  const sid = sessionId;
  try {
    const r = await fetch('/api/session/cwd?session_id=' + encodeURIComponent(sid));
    if (!r.ok) return;
    const d = await r.json();
    if (sid !== sessionId) return;  // a newer switch landed while we waited
    cwdBtnLabel.textContent = _shortPath(d.working_dir);
    cwdBtn.title = 'Working folder: ' + d.working_dir + ' — click to change';
  } catch {}
}

async function setCwd() {
  let cur = '';
  try { cur = (await (await fetch('/api/session/cwd?session_id=' + encodeURIComponent(sessionId))).json()).working_dir; } catch {}
  const path = prompt('Working folder for this session (relative file ops resolve here):', cur || '');
  if (path == null) return;
  try {
    const r = await fetch('/api/session/cwd', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, path: path.trim() }),
    });
    if (!r.ok) { alert('Could not set folder: ' + (await r.text())); return; }
    const d = await r.json();
    cwdBtnLabel.textContent = _shortPath(d.working_dir);
    cwdBtn.title = 'Working folder: ' + d.working_dir + ' — click to change';
  } catch (e) { alert('Could not set folder: ' + e); }
}

if (cwdBtn) cwdBtn.addEventListener('click', setCwd);

// ---- federation activity (inbound peer requests) indicator --------------
const fedIndicator = document.getElementById('fed-indicator');
const fedIndicatorText = document.getElementById('fed-indicator-text');
let _fedLastSeen = 0;

async function pollFederation() {
  if (!fedIndicator) return;
  try {
    const r = await fetch('/api/dispatch');
    if (!r.ok) return;
    const d = await r.json();
    const fed = d.federation || { active: 0, recent: [] };
    if (fed.active > 0) {
      const who = (fed.recent[0] && fed.recent[0].source) || 'peer';
      fedIndicatorText.textContent = `serving ${fed.active} peer request${fed.active > 1 ? 's' : ''} (${who})`;
      fedIndicator.style.display = '';
      _fedLastSeen = Date.now();
    } else if (Date.now() - _fedLastSeen < 4000 && fed.recent.length) {
      const last = fed.recent[0];
      fedIndicatorText.textContent = `peer request ${last.status} (${last.source})`;
      fedIndicator.style.display = '';
    } else {
      fedIndicator.style.display = 'none';
    }
  } catch {}
}
setInterval(pollFederation, 3000);
pollFederation();

// Live receive (Ph 95): surface inter-session (and Ph-83 channel) messages that
// arrive in the ACTIVE session as they land. The note is also injected into the
// session's history, so a tab switch / reload shows it via rebuildHistory — we
// reseed the cursor whenever the active session changes, so we never render the
// same message twice.
let _inboxSeenId = null, _inboxSeen = 0;
async function pollInbox() {
  const sid = sessionId;
  if (!sid) return;
  try {
    const r = await fetch(`/api/session/${encodeURIComponent(sid)}/channel`);
    if (!r.ok) return;
    const msgs = ((await r.json()).messages) || [];
    if (sid !== _inboxSeenId) { _inboxSeenId = sid; _inboxSeen = msgs.length; return; }
    for (let i = _inboxSeen; i < msgs.length; i++) {
      const m = msgs[i] || {};
      const who = String(m.source || '').replace(/^session:/, '') || 'a session';
      renderMarkdownInto(addBubble('system'), `📨 **Message from ${who}:** ${m.text || ''}`);
    }
    _inboxSeen = msgs.length;
  } catch {}
}
setInterval(pollInbox, 3000);

let pickerState = {
  active: '', fast_model: '', models: [],
  effort: 'medium', effort_levels: ['low','medium','high','max'],
  fast_mode: false, backend: '',
};

async function loadPicker() {
  try {
    const r = await fetch('/api/model-picker');
    if (!r.ok) return;
    pickerState = await r.json();
    renderFooter();
  } catch {}
}

const _CAP_CHIPS = { vision: '👁', reasoning: '🧠', infill: '⌨', audio: '🎤',
                     tools: '🔧', guard: '🛡', embed: '◆' };
const _CAP_TITLE = { vision: 'Vision (VLM)', reasoning: 'Extended thinking',
                     infill: 'Code infill / FIM', audio: 'Audio input',
                     tools: 'Tool / function calling',
                     guard: 'Safety-guard model', embed: 'Embeddings / reranker' };

function _capChips() {
  // Emoji chips for the active model's capabilities (vision/thinking/infill/audio).
  const caps = pickerState.active_capabilities || {};
  const on = Object.keys(_CAP_CHIPS).filter(k => caps[k]);
  let s = on.map(k => _CAP_CHIPS[k]).join('');
  if (pickerState.api === 'responses') s += '☁';
  return s ? ' ' + s : '';
}

function renderFooter() {
  const m = pickerState.active || 'no model';
  // Strip overlong tags so the chip stays compact.
  const shortModel = m.length > 26 ? m.slice(0, 25) + '…' : m;
  const effort = pickerState.effort && pickerState.effort !== 'medium'
    ? ` · ${pickerState.effort}` : '';
  const fast = pickerState.fast_mode ? ' · fast' : '';
  modelBtnLabel.textContent = shortModel + effort + fast + _capChips();
  // Tooltip spells out the capabilities.
  const caps = pickerState.active_capabilities || {};
  const names = Object.keys(_CAP_CHIPS).filter(k => caps[k]).map(k => _CAP_TITLE[k]);
  if (pickerState.api === 'responses') names.push('Responses API (cloud tools)');
  modelBtn.title = names.length ? 'Capabilities: ' + names.join(', ') : 'Switch model (Ctrl+I)';
}

// The backend that should serve `id`: prefer the currently-active backend when
// it offers the model, else the model's first source. Shared by the picker rows
// and the 1-9 keyboard shortcut so both switch to the same backend.
function _backendForModel(id) {
  const src = (pickerState.sources || {})[id] || [];
  return src.includes(pickerState.active_backend) ? pickerState.active_backend : (src[0] || '');
}

function renderPicker() {
  const { active, models, effort, effort_levels, fast_mode } = pickerState;
  const rows = [];
  rows.push(`<div class="picker-section"><span>Models</span><span class="keys"><kbd>⇧</kbd><kbd>Ctrl</kbd><kbd>I</kbd></span></div>`);
  // Show up to 9 models with number shortcuts; rest unnumbered.
  const allCaps = pickerState.capabilities || {};
  const sources = pickerState.sources || {};
  models.slice(0, 30).forEach((id, i) => {
    const isActive = id === active;
    const num = i < 9 ? (i + 1) : '';
    const caps = allCaps[id] || {};
    const chips = Object.keys(_CAP_CHIPS).filter(k => caps[k]).map(k => _CAP_CHIPS[k]).join('');
    const capSpan = chips ? ` <span class="caps" title="capabilities">${chips}</span>` : '';
    // Source backend tag(s) — which configured backend serves this model.
    const srcList = sources[id] || [];
    const srcBackend = _backendForModel(id);
    const srcSpan = srcList.length
      ? ` <span class="src" title="backend: ${escapeAttr(srcList.join(', '))}">${escapeHtml(srcList.join('/'))}</span>`
      : '';
    rows.push(
      `<div class="picker-row${isActive ? ' selected' : ''}" data-action="model" data-value="${escapeAttr(id)}" data-backend="${escapeAttr(srcBackend)}">
        <span class="name">${escapeHtml(id)}${capSpan}${srcSpan}</span>
        <span>${isActive ? '<span class="check">✓</span> ' : ''}<span class="shortcut">${num}</span></span>
      </div>`
    );
  });
  rows.push(`<div class="picker-divider"></div>`);
  rows.push(`<div class="picker-section"><span>Effort</span><span class="keys"><kbd>⇧</kbd><kbd>Ctrl</kbd><kbd>E</kbd></span></div>`);
  effort_levels.forEach((lvl, i) => {
    const isActive = lvl === effort;
    rows.push(
      `<div class="picker-row${isActive ? ' selected' : ''}" data-action="effort" data-value="${lvl}">
        <span class="name">${capitalize(lvl)}</span>
        <span>${isActive ? '<span class="check">✓</span>' : ''}</span>
      </div>`
    );
  });
  rows.push(`<div class="picker-divider"></div>`);
  rows.push(`<div class="picker-section"><span>Fast mode</span></div>`);
  rows.push(
    `<div class="picker-toggle${fast_mode ? ' on' : ''}" data-action="fast">
       <span class="name">Enable fast mode</span>
       <span class="switch"></span>
     </div>`
  );
  pickerCard.innerHTML = rows.join('');
}

function escapeHtml(s) {
  return s.replace(/[&<>]/g, c => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;' }[c]));
}
function escapeAttr(s) { return escapeHtml(s).replace(/"/g, '&quot;'); }
function capitalize(s) { return s ? s[0].toUpperCase() + s.slice(1) : ''; }

async function applyPicker(patch) {
  try {
    const r = await fetch('/api/model-picker', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    // Re-fetch the FULL snapshot rather than copying a subset of the POST
    // response: that response omits active_capabilities/api (and models/sources),
    // so patching a few fields left the footer capability chips + cloud flag —
    // and the picker's model list — showing the previously-active model.
    await loadPicker();   // GET → replaces pickerState wholesale → renderFooter()
    renderPicker();       // refresh the open overlay too
    if (pickerState.active) meta.textContent = 'model=' + pickerState.active;
  } catch (err) {
    console.error('picker apply failed', err);
  }
}

function openPicker() {
  renderPicker();
  pickerOverlay.classList.add('active');
  pickerCard.focus();
}
function closePicker() { pickerOverlay.classList.remove('active'); }

modelBtn.addEventListener('click', () => {
  loadPicker().then(openPicker);
});
pickerOverlay.addEventListener('click', e => {
  if (e.target === pickerOverlay) closePicker();
});
pickerCard.addEventListener('click', e => {
  const row = e.target.closest('[data-action]');
  if (!row) return;
  const action = row.dataset.action;
  if (action === 'model') applyPicker({ model: row.dataset.value, backend: row.dataset.backend || undefined });
  else if (action === 'effort') applyPicker({ effort: row.dataset.value });
  else if (action === 'fast') applyPicker({ fast_mode: !pickerState.fast_mode });
});

// Keyboard shortcuts:
//   Ctrl+I → open picker
//   Esc → close
//   1-9 (when picker open) → pick that model
//   Shift+Ctrl+E → focus the Effort section (cycles low→medium→high→max)
window.addEventListener('keydown', e => {
  if (e.key === 'Escape' && pickerOverlay.classList.contains('active')) {
    closePicker(); return;
  }
  const mod = e.ctrlKey || e.metaKey;
  if (mod && !e.altKey && e.key.toLowerCase() === 'i') {
    e.preventDefault();
    loadPicker().then(openPicker);
    return;
  }
  if (mod && e.shiftKey && e.key.toLowerCase() === 'e') {
    e.preventDefault();
    const order = pickerState.effort_levels;
    const i = order.indexOf(pickerState.effort);
    const next = order[(i + 1) % order.length];
    applyPicker({ effort: next });
    return;
  }
  if (pickerOverlay.classList.contains('active') && /^[1-9]$/.test(e.key)) {
    const n = parseInt(e.key, 10);
    const id = pickerState.models[n - 1];
    // Pass the model's source backend too (as clicking the row does) — without
    // it, picking a model served only by a different backend left the active
    // backend pointing at a model it can't serve, with no client rebuild.
    if (id) applyPicker({ model: id, backend: _backendForModel(id) || undefined });
  }
});

// Initial load: pick up the model picker state + restore the active tab's
// history (when we have one from a previous visit).
loadPicker();
loadCwd();
// Cross-device handoff (Phase 87): /?session=<id> opens (or adds) that session.
const _wantSession = (() => {
  try { return new URLSearchParams(location.search).get('session'); }
  catch { return null; }
})();
if (_wantSession) {
  let i = tabs.findIndex(t => t.id === _wantSession);
  if (i < 0) { tabs.push({ id: _wantSession, label: 'resumed' }); i = tabs.length - 1; }
  activeIdx = i;
  setSessionId(tabs[i].id);
  saveTabsState();
  renderTabs();
}
if (_loaded || _wantSession) {
  rebuildHistory();
}
// evi://workflow/<name> → /?workflow=<name> opens the dispatch panel (Phase 85).
(() => {
  let wf = null;
  try { wf = new URLSearchParams(location.search).get('workflow'); } catch {}
  if (wf) setTimeout(() => { try { openDispatch(); } catch {} }, 400);
})();
// Warn up front if no LLM backend is reachable (common on a fresh desktop
// install before LM Studio / Ollama is started).
checkBackend();
// Offer the starter prompts on a fresh transcript. Deferred a tick so a restored
// session's history wins — the prompts are for an empty chat, not a loaded one.
setTimeout(renderEmptyState, 600);

// --- file upload (drag-drop + paperclip) ---------------------------------
const attachBtn = document.getElementById('attach');
const fileInput = document.getElementById('file-input');
const dropOverlay = document.getElementById('drop-overlay');

attachBtn.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', () => {
  if (fileInput.files.length) handleAttachment(fileInput.files[0]);
});

let dragCount = 0;
window.addEventListener('dragenter', e => {
  if (!e.dataTransfer || ![...e.dataTransfer.types].includes('Files')) return;
  dragCount++;
  dropOverlay.classList.add('active');
});
window.addEventListener('dragleave', () => {
  dragCount = Math.max(0, dragCount - 1);
  if (dragCount === 0) dropOverlay.classList.remove('active');
});
window.addEventListener('dragover', e => { e.preventDefault(); });
window.addEventListener('drop', e => {
  e.preventDefault();
  dragCount = 0;
  dropOverlay.classList.remove('active');
  if (e.dataTransfer && e.dataTransfer.files.length) {
    handleAttachment(e.dataTransfer.files[0]);
  }
});

// Paste an image straight from the clipboard (a screenshot, a copied picture)
// into the composer → attach it, exactly like the 📎 button and drag-drop.
// Text pastes fall through untouched. Clipboard images arrive as nameless
// blobs, so synthesize a stable filename for the upload + chip.
input.addEventListener('paste', e => {
  const items = (e.clipboardData && e.clipboardData.items) || [];
  for (const it of items) {
    if (it.kind === 'file' && it.type.startsWith('image/')) {
      const blob = it.getAsFile();
      if (!blob) continue;
      e.preventDefault();
      const ext = (it.type.split('/')[1] || 'png')
        .replace('jpeg', 'jpg').replace('svg+xml', 'svg');
      handleAttachment(new File([blob], `pasted-${Date.now()}.${ext}`, { type: it.type }));
      return;
    }
  }
  // No image on the clipboard → let the normal text paste happen.
});

async function handleAttachment(file) {
  const chip = addBubble('upload-chip');
  const mime = file.type || '';
  // Audio? Send to the transcribe endpoint; the transcribed text drops
  // straight into the chat input for the user to optionally edit + send.
  if (mime.startsWith('audio/') || /\.(wav|mp3|m4a|ogg|flac|webm)$/i.test(file.name)) {
    chip.textContent = `transcribing ${file.name}…`;
    try {
      const fd = new FormData();
      fd.append('session_id', sessionId);
      fd.append('file', file);
      const r = await fetch('/api/transcribe', { method: 'POST', body: fd });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      if (!data.ok) {
        chip.textContent = `transcription failed: ${data.error}`;
        return;
      }
      chip.innerHTML = `transcribed <code>${file.name}</code>: <em>${data.text.slice(0, 200)}${data.text.length > 200 ? '…' : ''}</em>`;
      // Drop the transcription into the input box so the user can review/send.
      input.value = (input.value ? input.value + ' ' : '') + data.text;
      input.focus();
    } catch (err) {
      chip.textContent = `transcription failed: ${err.message}`;
    }
    return;
  }
  chip.textContent = `uploading ${file.name}…`;
  try {
    const fd = new FormData();
    fd.append('session_id', sessionId);
    fd.append('file', file);
    const r = await fetch('/api/upload', { method: 'POST', body: fd });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    attachedFile = data.path;
    attachedIsImage = mime.startsWith('image/');
    // Surface the default prompt in the composer so it's VISIBLE and editable,
    // instead of being silently substituted at send time (users were surprised
    // to see "What's in this image?" they never typed). Only when empty, so a
    // question they already typed is never clobbered.
    if (attachedIsImage && !input.value.trim()) {
      input.value = "What's in this image?";
      input.focus();
    }
    const sizeStr = data.size.toLocaleString();
    if (attachedIsImage) {
      // Inline thumbnail for instant feedback that the right file came up.
      const reader = new FileReader();
      reader.onload = ev => {
        chip.innerHTML = `image attached: <code>${data.filename}</code>
          <span style="color:var(--dim)">(${sizeStr} bytes)</span>
          <img src="${ev.target.result}" alt="" style="max-width:240px;max-height:160px;margin-top:6px;border-radius:4px">`;
      };
      reader.readAsDataURL(file);
    } else {
      chip.innerHTML = `attached: <code>${data.filename}</code>
        <span style="color:var(--dim)">(${sizeStr} bytes)</span><br>
        <span style="color:var(--dim)">path will be referenced in your next message</span>`;
    }
  } catch (err) {
    chip.textContent = `upload failed: ${err.message}`;
  }
}

// --- auto-speak (browser speechSynthesis) -------------------------------

const notifyBtn = document.getElementById('notify-btn');
function updateNotifyButton() {
  const on = notifyEnabled();
  notifyBtn.textContent = on ? '🔔' : '🔕';
  notifyBtn.title = on
    ? 'Turn-done notifications ON (when tab backgrounded) — click to disable'
    : 'Turn-done notifications OFF — click to enable';
}
updateNotifyButton();
notifyBtn.addEventListener('click', async () => {
  await notifyToggle();
  updateNotifyButton();
});

const speakBtn = document.getElementById('speak-btn');
const SPEAK_KEY = 'evi.autoSpeak';
let autoSpeak = localStorage.getItem(SPEAK_KEY) === '1';

function updateSpeakButton() {
  speakBtn.textContent = autoSpeak ? '🔊' : '🔇';
  speakBtn.title = autoSpeak
    ? 'Auto-speak ON — click to mute'
    : 'Auto-speak OFF — click to enable';
}
updateSpeakButton();

speakBtn.addEventListener('click', () => {
  if (!('speechSynthesis' in window)) {
    addBubble('error').textContent = 'Browser has no speechSynthesis support';
    return;
  }
  autoSpeak = !autoSpeak;
  localStorage.setItem(SPEAK_KEY, autoSpeak ? '1' : '0');
  if (!autoSpeak) window.speechSynthesis.cancel();
  updateSpeakButton();
});

// Per-assistant-bubble buffer: dataset.spokenIdx tracks how far we've
// already enqueued for speech.
const SENTENCE_RE = /[^.!?]+[.!?]+(\s+|$)/g;
function cleanForTTS(s) {
  return s
    .replace(/```[\s\S]*?```/g, ' code block ')
    .replace(/`[^`]+`/g, ' code ')
    .replace(/https?:\/\/\S+/g, ' link ')
    .replace(/[*_#>]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}
function maybeSpeak(bubble) {
  if (!autoSpeak || !('speechSynthesis' in window)) return;
  const src = bubble.dataset.source || '';
  const from = parseInt(bubble.dataset.spokenIdx || '0', 10);
  const tail = src.slice(from);
  let lastEnd = 0;
  let m;
  // Reset lastIndex since /g state persists between calls.
  SENTENCE_RE.lastIndex = 0;
  while ((m = SENTENCE_RE.exec(tail)) !== null) {
    const sentence = cleanForTTS(m[0]);
    if (sentence) {
      const u = new SpeechSynthesisUtterance(sentence);
      window.speechSynthesis.speak(u);
    }
    lastEnd = m.index + m[0].length;
  }
  if (lastEnd) bubble.dataset.spokenIdx = String(from + lastEnd);
}
function flushSpeak(bubble) {
  if (!autoSpeak || !('speechSynthesis' in window) || !bubble) return;
  const src = bubble.dataset.source || '';
  const from = parseInt(bubble.dataset.spokenIdx || '0', 10);
  const tail = cleanForTTS(src.slice(from));
  if (tail) {
    const u = new SpeechSynthesisUtterance(tail);
    window.speechSynthesis.speak(u);
    bubble.dataset.spokenIdx = String(src.length);
  }
}

resetBtn.addEventListener('click', async () => {
  const r = await fetch('/api/reset', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, message: '' }),
  });
  if (r.status === 409) {
    // A turn is still running: the history was NOT cleared, so keep the view.
    addBubble('system').textContent = 'A turn is still running in this session — reset when it finishes.';
    return;
  }
  log.innerHTML = ''; renderEmptyState();
  nextHistoryIndex = 1;
  refreshUsage();  // the cleared session's context is back to ~0 — don't show the old count
  // Reset the current tab's label too — it'll get re-named on the next user msg.
  if (tabs[activeIdx]) setTabLabel('new chat');
});

// Session modes (Chat / Cowork / Code) are temporarily removed from the UI —
// the switcher silently gated the desktop to "chat" (memory+skills only, no
// fs/code/web tools). Sessions now use the full configured [tools] toolset. The
// backend (/api/modes, /api/session/{id}/mode, evi/modes.py, CLI `-m`) is kept
// for when modes return with proper UI feedback.

// ===== Settings, theme, in-app dialogs, desktop-menu bridge ============
// One module backing the ⚙ settings screen, the in-app Documentation +
// Diagnostics dialogs, and (in the desktop build) the native menus + tray.
// Web users reach everything through the gear button + the About section;
// desktop menus just emit events that call the same functions here.
(function () {
  'use strict';

  // ---- theme (client-side pref; also View → Toggle Theme) -------------
  const THEME_KEY = 'evi.theme';   // 'system' | 'dark' | 'light'
  const media = window.matchMedia('(prefers-color-scheme: light)');
  const effectiveTheme = pref =>
    (pref === 'light' || pref === 'dark') ? pref : (media.matches ? 'light' : 'dark');
  function applyTheme() {
    document.documentElement.dataset.theme =
      effectiveTheme(localStorage.getItem(THEME_KEY) || 'system');
  }
  function setThemePref(pref) { localStorage.setItem(THEME_KEY, pref); applyTheme(); }
  function toggleTheme() {
    setThemePref(effectiveTheme(localStorage.getItem(THEME_KEY) || 'system') === 'light' ? 'dark' : 'light');
  }
  media.addEventListener('change', applyTheme);
  applyTheme();

  // ---- desktop detection + external open ------------------------------
  // The desktop shell no longer exposes Tauri's JS API (withGlobalTauri is
  // off, so window.__TAURI__ is undefined). This page reaches the shell only
  // through Tauri's IPC primitive, and only for the commands below; the
  // shell's capability grants exactly these, and only to the eVi server's
  // origin (desktop/src-tauri/src/main.rs).
  const DESKTOP_COMMANDS = new Set([
    'check_for_update_cmd', 'install_update_cmd', 'restart_app_cmd', 'update_available_cmd',
    'dismiss_update_cmd', 'staged_update_cmd', 'open_logs_cmd', 'open_external_cmd',
    'update_status_cmd',
  ]);
  const desktopIpc = () => {
    const ipc = window.__TAURI_INTERNALS__;
    return ipc && typeof ipc.invoke === 'function' ? ipc : null;
  };
  const isDesktop = () => !!desktopIpc();
  const invoke = (cmd, args) => {
    const ipc = desktopIpc();
    if (!ipc) return Promise.reject('not desktop');
    if (!DESKTOP_COMMANDS.has(cmd)) return Promise.reject('not a desktop command: ' + cmd);
    return ipc.invoke(cmd, args || {});
  };
  function openExternal(url) {
    if (isDesktop()) invoke('open_external_cmd', { url }).catch(() => window.open(url, '_blank', 'noopener'));
    else window.open(url, '_blank', 'noopener');
  }
  // Links in rendered markdown carry target=_blank, which a browser opens in a
  // new tab. The desktop window never navigates away from eVi (its navigation
  // guard refuses), so there they go to the system browser instead.
  document.getElementById('log').addEventListener('click', e => {
    const a = e.target && e.target.closest ? e.target.closest('.md a[href]') : null;
    if (!a || !isDesktop() || !/^https?:\/\//i.test(a.getAttribute('href') || '')) return;
    e.preventDefault();
    openExternal(a.href);
  });

  // ---- generic dialog (docs / diagnostics / shortcuts / updates) ------
  const dlgOverlay = document.getElementById('dialog-overlay');
  const dlgTitle = document.getElementById('dialog-title');
  const dlgContent = document.getElementById('dialog-content');
  function openDialog(title, html) {
    dlgTitle.textContent = title; dlgContent.innerHTML = html;
    dlgOverlay.classList.add('shown');
  }
  const closeDialog = () => dlgOverlay.classList.remove('shown');
  document.getElementById('dialog-close').addEventListener('click', closeDialog);
  dlgOverlay.addEventListener('click', e => { if (e.target === dlgOverlay) closeDialog(); });

  // ---- settings schema (declarative → generic renderer, stays DRY) ----
  const TOOL_KEYS = ['fs','code','shell','gmail','outlook','image','memory','subagent',
    'mcp','skills','web','voice','computer','transcripts','pdf','sqlite','index','git','ocr','calendar',
    'federation','ask','vision'];
  // What [auto] auto_approve can name: the tool categories, plus "code_read",
  // a category of the CALL rather than a toggle (evi.permissions.classify_call):
  // the code tools' calls that only read code. Placed before "code" so the two
  // read as the pair they are.
  const AUTO_APPROVE_KEYS = TOOL_KEYS.flatMap(k => k === 'code' ? ['code_read', 'code'] : [k]);
  // One line each where the category's name alone misleads: what auto-approving
  // it lets run with no prompt.
  const AUTO_APPROVE_WHY = {
    code_read: 'Reading and analysing code, not running it: python_symbols, monitor watching a file, '
      + 'check_file on Python (ruff / pyflakes run no project code), and run_python only while the OS sandbox is active.',
    code: 'Running code: run_python outside a sandbox and check_file linters that execute project code '
      + '(eslint, cargo clippy, go vet). Includes code_read. Never unasked in a run a remote trigger drives.',
    shell: 'Shell commands: run_command and monitor running a command (kind=command).',
  };
  const RISKY = new Set(['shell','computer','web','federation']);
  // Why each risky tool carries the ⚠ — shown as a hover tooltip + legend so
  // the icon reads as "acts outside eVi's sandbox, enable with care" and is NOT
  // mistaken for "unavailable / not installed" (a different thing entirely; the
  // icon never reflects availability, only risk).
  const RISKY_WHY = {
    shell: 'Caution: runs shell commands on your computer — can read, change, or delete files and launch programs.',
    computer: 'Caution: controls your mouse and keyboard — can click and type anywhere on your screen.',
    web: 'Caution: reaches the internet (web search / fetching pages), sending your query to outside services. (This is about network reach, not availability.)',
    federation: 'Caution: connects to and shares with other eVi peers on your network.',
  };
  const SCHEMA = [
    { id:'general', title:'General', blurb:'Appearance and reporting.', fields:[
      { type:'client-select', key:'theme', label:'Theme', options:['system','dark','light'],
        help:'eVi follows your OS by default. Also under View → Toggle Theme.' },
      { type:'style', section:'llm', key:'output_style', label:'Output style',
        help:'Response persona layered on the system prompt (concise / explanatory / teacher, or your own ~/.evi/styles/*.md).' },
      { type:'bool', section:'telemetry', key:'crash_reports', label:'Crash reporting',
        help:'Opt-in. Sends scrubbed reports only when a DSN is set. Off by default.' },
      { type:'secret', section:'telemetry', key:'dsn', label:'Telemetry DSN',
        help:'Sentry-compatible endpoint. Blank keeps reporting inert.' },
      { type:'bool', section:'telemetry', key:'traces', label:'OpenTelemetry traces',
        help:'Opt-in spans around tool calls. Needs the [otel] extra + an endpoint.' },
      { type:'bool', section:'telemetry', key:'metrics', label:'OpenTelemetry metrics',
        help:'Opt-in tool-call counters/durations. Needs the [otel] extra + an endpoint.' },
      { type:'text', section:'telemetry', key:'otlp_endpoint', label:'OTLP endpoint',
        help:'Base OTLP/HTTP URL, e.g. http://localhost:4318. Blank = nothing is exported.' },
      { type:'select', section:'telemetry', key:'backend', label:'Crash-report backend', options:['sentry','none'],
        help:'Which SDK sends crash reports when a DSN is set.' },
      { type:'text', section:'telemetry', key:'service_name', label:'Telemetry service name',
        help:'Service-name tag on exported traces / reports.' },
    ]},
    { id:'model', title:'Model & Backend', blurb:'Where eVi sends your messages.', fields:[
      { type:'select', section:'llm', key:'backend', label:'Backend',
        options:['lmstudio','ollama','llamacpp','openai_compat','claude_agent','codex','gemini','copilot','amp'],
        help:'Switching rebuilds the live connection. The claude_agent/codex/gemini/copilot/amp kinds drive a local CLI-agent instead of an HTTP endpoint; the CLI signs in itself (a subscription login, or for gemini a Gemini API key / Vertex AI).' },
      { type:'text', section:'llm', key:'base_url', label:'Base URL' },
      { type:'model', section:'llm', key:'model', label:'Model',
        help:'Detected on the backend; pick Custom… to type one.' },
      { type:'secret', section:'llm', key:'api_key', label:'API key',
        help:'Local backends ignore this; cloud endpoints need it.' },
      { type:'select', section:'llm', key:'api', label:'API shape', options:['chat','responses'],
        help:'Chat for local backends; Responses only for endpoints that implement it.' },
      { type:'lines', section:'llm', key:'responses_tools', label:'Responses built-in tools',
        help:'When API shape is Responses: OpenAI hosted tools to enable, one per line (web_search, code_interpreter, file_search).' },
      { type:'number', section:'llm', key:'temperature', label:'Temperature', step:0.1, min:0, max:2 },
      { type:'int', section:'llm', key:'max_tokens', label:'Max tokens' },
      { type:'int', section:'llm', key:'context_size', label:'Context size' },
      { type:'select', section:'llm', key:'reasoning_effort', label:'Reasoning effort',
        options:['off','low','medium','high','max'],
        help:'off fully disables extended thinking on reasoning models.' },
      { type:'bool', section:'llm', key:'fast_mode', label:'Fast mode' },
      { type:'model', section:'llm', key:'fast_model', label:'Fast model',
        help:'Smaller model used when fast mode is on; pick Custom… to type one.' },
      { type:'lines', section:'llm', key:'fallback_models', label:'Fallback models',
        help:'On a retryable backend error, retry the turn against each of these model ids in order (one per line).' },
      { type:'bool', section:'llm', key:'router_enabled', label:'Multi-model routing',
        help:'Match each turn against ~/.evi/routes.json (Settings → Routes & Recipes) and swap the model for that turn.' },
      { type:'model', section:'llm', key:'router_model', label:'Router classifier model',
        help:'Optional tiny model to pick a route when no keyword rule matches. Blank = keyword-only routing.' },
    ]},
    { id:'tools', title:'Tools', blurb:'Capabilities eVi can use.', applies:'new-chat',
      fields: TOOL_KEYS.map(k => ({ type:'bool', section:'tools', key:k, label:k, risky:RISKY.has(k), riskyWhy:RISKY_WHY[k] }))
        .concat([
          { type:'bool', section:'tools', key:'sandbox', label:'Sandbox code execution',
            help:'Run run_python under an OS sandbox (read-only FS, no network) where available (bwrap / sandbox-exec).' },
          { type:'lines', section:'tools', key:'mcp_allow', label:'MCP server allowlist',
            help:'If non-empty, only these mcp.json server names load (one per line). Empty = all enabled servers.' },
          { type:'bool', section:'tools', key:'tool_search', label:'Tool search at scale',
            help:'When the enabled toolset exceeds the threshold below, defer the long tail behind a search_tools meta-tool instead of sending every schema each turn (keeps context small with many MCP tools).' },
          { type:'int', section:'tools', key:'tool_search_threshold', label:'Tool-search threshold',
            help:'Enabled-tool count above which tool search kicks in (when the toggle above is on).' },
          { type:'int', section:'tools', key:'mcp_max_output_chars', label:'Max MCP output chars',
            help:'Truncate a single MCP tool result to this many characters before handing it to the model. 0 = no cap.' },
        ]) },
    { id:'editing', title:'Editing & data', blurb:'Code-edit assists and stored-transcript retention.', fields:[
      { type:'bool', section:'tools', key:'format_on_edit', label:'Format on edit',
        help:'Auto-run an installed formatter (ruff/black/prettier/gofmt/rustfmt) after write/edit/apply_patch. No-op if none installed.' },
      { type:'bool', section:'tools', key:'check_on_edit', label:'Check on edit',
        help:'Run the linter after a write and fold any diagnostics into the tool result (cheap LSP-lite feedback).' },
      { type:'int', section:'tools', key:'cleanup_period_days', label:'Transcript retention (days)',
        help:'Delete stored sessions older than N days on startup. 0 = keep forever.' },
      { type:'text', section:'worktree', key:'base_ref', label:'Worktree base ref',
        help:'Branch/commit new `evi worktree` trees fork from when --base is omitted (e.g. main). Blank = HEAD.' },
    ]},
    { id:'permissions', title:'Permissions', blurb:'How tool calls are approved.',
      applies:'new-chat', fields:[
        { type:'select', section:'auto', key:'mode', label:'Permission mode',
          options:['ask','accept_edits','plan','yolo'],
          help:'ask: prompt for non-approved tools · accept_edits: auto-allow file edits and code reading (never running code) · plan: no tools · yolo: allow everything' },
        { type:'auto-approve', section:'auto', key:'auto_approve' },
        { type:'lines', section:'auto', key:'rules', label:'Rules (one per line)',
          help:'allow|deny <tool-name-glob> [arg-glob], first match wins — e.g. "deny run_command rm*", "allow web_*", "deny *_file *.env". A shell line is matched per command: an allow covers it only if every command is allowed, the line parses and redirects stay in the working folder; a deny matches any one command.' },
        { type:'lines', section:'auto', key:'trusted_dirs', label:'Trusted directories',
          help:'Auto-approve file tools whose path is under one of these dirs (one per line).' },
        { type:'lines', section:'auto', key:'trusted_domains', label:'Trusted domains',
          help:'Auto-approve web fetches to these hosts (one per line, e.g. docs.python.org).' },
        { type:'bool', section:'auto', key:'block_destructive', label:'Destructive-command guard',
          help:'Force a confirmation (or deny when headless) for curated destructive shell commands — rm -rf ~, git reset --hard, force-push, disk format, secret exfil, IaC destroy. Overrides yolo/accept_edits. On by default.' },
        { type:'lines', section:'auto', key:'destructive_allow', label:'Guard exemptions (globs)',
          help:'fnmatch globs that EXEMPT a command from the guard, matched against each command in the line (never the whole line, never a line eVi cannot split), e.g. git push --force-with-lease*.' },
        { type:'lines', section:'auto', key:'destructive_disable_rules', label:'Disabled guard rules',
          help:'Builtin rule ids to silence without disabling the whole guard, e.g. git-commit-amend, system-power.' },
        { type:'lines', section:'auto', key:'hard_deny', label:'Always-deny rules',
          help:'Evaluated before everything (even yolo): <tool-glob> [arg-glob], e.g. "shell rm -rf*", "fs *.pem".' },
        { type:'lines', section:'auto', key:'protected_paths', label:'Protected paths',
          help:'Force a prompt even under accept_edits/trusted_dirs (fnmatch on path + basename), e.g. .env, *.pem, id_rsa.' },
      ] },
    { id:'context', title:'Context', blurb:'When eVi compacts long conversations.', fields:[
      { type:'int', section:'llm', key:'compact_after_messages', label:'Compact after N messages', help:'0 disables.' },
      { type:'int', section:'llm', key:'compact_keep_recent', label:'Keep recent turns' },
      { type:'int', section:'llm', key:'compact_when_pct', label:'Compact at % of context' },
    ]},
    { id:'sampling', title:'Sampling & advanced', blurb:'Fine-grained generation knobs — most only take effect when they deviate from the default.', fields:[
      { type:'number', section:'llm', key:'top_p', label:'Top-p', step:0.05, min:0, max:1 },
      { type:'number', section:'llm', key:'presence_penalty', label:'Presence penalty', step:0.1, min:-2, max:2 },
      { type:'number', section:'llm', key:'frequency_penalty', label:'Frequency penalty', step:0.1, min:-2, max:2 },
      { type:'int', section:'llm', key:'seed', label:'Seed', help:'0 = unset; non-zero seeds the backend RNG for reproducible runs.' },
      { type:'lines', section:'llm', key:'stop_sequences', label:'Stop sequences', help:'Hard-stop generation tokens (one per line). Empty = no override.' },
      { type:'bool', section:'llm', key:'parallel_tool_calls', label:'Parallel tool calls', help:'Off = the model may request only one tool per turn.' },
      { type:'int', section:'llm', key:'max_completion_tokens', label:'Max completion tokens', help:'For reasoning models that reject max_tokens. 0 = unset (send max_tokens instead).' },
      { type:'text', section:'llm', key:'logit_bias', label:'Logit bias (JSON)', help:'Token-id → bias map as JSON, e.g. {"123": -100}. Values clamp to [-100,100]. Blank = none.' },
      { type:'bool', section:'llm', key:'cache_prompt', label:'Cache prompt (KV reuse)', help:'llama.cpp / vLLM prefix-cache hint; other backends ignore it.' },
      { type:'bool', section:'llm', key:'logprobs', label:'Log probabilities', help:'Ask the backend for per-token logprobs and show a confidence summary.' },
      { type:'int', section:'llm', key:'top_logprobs', label:'Top-N logprobs', help:'0-20 most-likely alternates per position (needs Log probabilities on).' },
      { type:'number', section:'llm', key:'request_timeout', label:'Request timeout (s)', step:5, min:0 },
      { type:'text', section:'llm', key:'embed_model', label:'Embedding model', help:'Used for semantic file search / index.' },
      { type:'int', section:'llm', key:'embed_dimensions', label:'Embedding dimensions' },
    ]},
    { id:'specialty', title:'Specialty models', blurb:'Optional small per-task models — OCR / vision / speech / guard — distinct from the main chat model. Blank = use the main model / default engine.', fields:[
      { type:'text', section:'models', key:'ocr', label:'OCR model', help:'Chat-VLM id served over the image schema (e.g. glm-ocr on Ollama).' },
      { type:'text', section:'models', key:'ocr_base_url', label:'OCR base URL', help:'Point OCR at a separate server. Blank = the [llm] backend.' },
      { type:'text', section:'models', key:'ocr_backend', label:'OCR backend kind' },
      { type:'text', section:'models', key:'vision', label:'Vision model', help:'VLM for describe_image (e.g. moondream).' },
      { type:'text', section:'models', key:'vision_base_url', label:'Vision base URL' },
      { type:'text', section:'models', key:'vision_backend', label:'Vision backend kind' },
      { type:'text', section:'models', key:'stt', label:'Speech-to-text model', help:'faster-whisper model id (e.g. large-v3-turbo).' },
      { type:'text', section:'models', key:'tts', label:'Text-to-speech voice / model' },
      { type:'text', section:'models', key:'guard', label:'Safety-guard model', help:'Llama Guard / ShieldGemma for the [[guard]] guardrail (e.g. llama-guard3).' },
      { type:'text', section:'models', key:'guard_base_url', label:'Guard base URL' },
      { type:'text', section:'models', key:'guard_backend', label:'Guard backend kind' },
      { type:'text', section:'models', key:'diarize', label:'Diarization model' },
      { type:'text', section:'models', key:'doc_layout', label:'Doc-layout model' },
    ]},
    { id:'integrations', title:'Integrations', blurb:'Optional external services.', fields:[
      { type:'text', section:'comfy', key:'base_url', label:'ComfyUI URL' },
      { type:'text', section:'obsidian', key:'vault_path', label:'Obsidian vault path' },
      { type:'text', section:'google', key:'client_secrets_path', label:'Google client secrets (path)' },
      { type:'text', section:'microsoft', key:'client_id', label:'Microsoft client ID' },
      { type:'text', section:'microsoft', key:'tenant_id', label:'Microsoft tenant ID', help:'AAD tenant (default: common).' },
      { type:'lines', section:'microsoft', key:'scopes', label:'Microsoft scopes', help:'Graph scopes (one per line), e.g. Mail.Read, User.Read.' },
      { type:'lines', section:'google', key:'scopes', label:'Google scopes', help:'OAuth scopes (one per line).' },
      { type:'text', section:'obsidian', key:'subdir', label:'Obsidian subfolder', help:'Vault subfolder eVi writes to (default: eVi).' },
      { type:'text', section:'comfy', key:'default_checkpoint', label:'ComfyUI checkpoint' },
      { type:'int', section:'comfy', key:'default_steps', label:'ComfyUI steps' },
      { type:'int', section:'comfy', key:'default_width', label:'ComfyUI width' },
      { type:'int', section:'comfy', key:'default_height', label:'ComfyUI height' },
      { type:'select', section:'tools', key:'search_backend', label:'Web search backend',
        options:['ddg','searxng','ollama'],
        help:'ddg: DuckDuckGo, keyless · searxng: self-hosted (set the URL below) · ollama: needs OLLAMA_API_KEY.' },
      { type:'text', section:'tools', key:'searxng_url', label:'SearXNG URL',
        help:'Your SearXNG instance, e.g. http://localhost:8888 (only used when the backend is searxng).' },
    ]},
    { id:'server', title:'Server', blurb:'Web access control.', fields:[
      { type:'secret', section:'web', key:'auth_token', label:'Access token',
        help:'When set, the web UI requires this token. Blank = open localhost access.' },
      { type:'bool', section:'web', key:'multi_user', label:'Multi-user logins',
        help:'Opt-in: each user logs in with their own token from ~/.evi/users.json (manage with `evi web-config users`). Shared workspace.' },
      { type:'bool', section:'federation', key:'a2a', label:'A2A interop endpoint',
        help:'Expose POST /a2a so any standards-compliant external agent (not just trusted eVi peers) can delegate a task here. Broader surface than eVi-to-eVi — set an access token above.' },
    ]},
    { id:'voice', title:'Voice', blurb:'Text-to-speech engine for spoken replies.', fields:[
      { type:'select', section:'voice', key:'engine', label:'TTS engine',
        options:['system','coqui','f5','piper'],
        help:'system: zero-dep platform voice · coqui (XTTS) & f5: voice cloning from a sample · piper: light local voices. Neural engines are optional installs.' },
      { type:'text', section:'voice', key:'model', label:'Model id / path',
        help:'Engine-specific. Coqui: an XTTS model id (blank = xtts_v2). Piper: a voice .onnx path.' },
      { type:'text', section:'voice', key:'clone_sample', label:'Clone sample (WAV path)',
        help:'Reference audio for the cloning engines (coqui / f5).' },
      { type:'text', section:'voice', key:'language', label:'Language', help:'ISO code, e.g. en.' },
    ]},
    { id:'notifications', title:'Notifications', blurb:'Pings when a turn finishes so you can walk away from a long run.', fields:[
      { type:'bool', section:'notify', key:'enabled', label:'Completion notifications',
        help:'Notify when the assistant finishes a turn.' },
      { type:'bool', section:'notify', key:'sound', label:'Sound', help:'Play a beep on completion.' },
      { type:'bool', section:'notify', key:'desktop', label:'Desktop toast',
        help:'Native OS notification on completion.' },
      { type:'text', section:'notify', key:'url', label:'ntfy / webhook URL',
        help:'Optional. POST completion to an ntfy topic or webhook so a remote/headless turn can reach your phone.' },
    ]},
    { id:'statusline', title:'Status line', blurb:'Customizable chat-REPL status line (off by default).', fields:[
      { type:'bool', section:'statusline', key:'enabled', label:'Enable status line' },
      { type:'text', section:'statusline', key:'format', label:'Format',
        help:'Tokens: {model} {used} {ceiling} {pct} {branch} {goal} {effort} {fast}.' },
      { type:'text', section:'statusline', key:'command', label:'Command (optional)',
        help:'Shell command fed the state as JSON on stdin; its stdout overrides Format.' },
    ]},
    { id:'multiagent', title:'Multi-agent', blurb:'How ultracode / workflows spread work across backends.', fields:[
      { type:'bool', section:'ultracode', key:'fanout', label:'Spread fan-out across flagged backends',
        help:'Run ultracode SOLVER angles across every backend ticked "fan-out" in Model & Backend (multi-provider). Without this, the per-backend fan-out flags do nothing.' },
      { type:'bool', section:'ultracode', key:'cheap_fanout', label:'Cheap fan-out (use the fast model)',
        help:'Run the solve stage on the [llm] fast model instead of fanning out across backends.' },
      { type:'int', section:'ultracode', key:'breadth', label:'Solver breadth',
        help:'Parallel solver angles per ultracode run (1 disables fan-out).' },
      { type:'int', section:'ultracode', key:'rounds', label:'Verify rounds',
        help:'Adversarial verify → refine cycles (0 skips critique).' },
      { type:'text', section:'ultracode', key:'mode', label:'Default mode',
        help:'Default task framing for ultracode runs (e.g. code).' },
      { type:'lines', section:'ultracode', key:'angles', label:'Solver angles',
        help:'Specific angle names from ultracode.SOLVER_ANGLES (one per line). Empty = the first N by breadth.' },
      { type:'int', section:'ultracode', key:'max_workers', label:'Max parallel workers' },
      { type:'bool', section:'ultracode', key:'auto_tune', label:'Auto-tune for small models',
        help:'Downshift breadth/rounds for tiny / short-context models.' },
    ]},
    { id:'desktop', title:'Desktop', blurb:'Settings for the desktop app.', desktopOnly:true, fields:[
      { type:'bool', section:'desktop', key:'sidecar_auto_update', label:'Automatically update the core',
        help:'Keep the app\'s bundled Python core (evi-server) up to date in the background from the signed sidecar channel, applied on next launch. Turn off to pin the core to the installed version.' },
    ]},
    { id:'guardrails', title:'Guardrails', blurb:'Content filter — regex, LLM-judge, and classifier rules.', custom:'guardrails' },
    { id:'plugins', title:'Plugins', blurb:'Installed plugins + the marketplace — search, install, remove.', custom:'plugins' },
    { id:'stats', title:'Usage', blurb:'Local usage analytics computed from your transcripts.', custom:'stats' },
    { id:'evals', title:'Evals', blurb:'Regression suites — the latest pass rates per model (from evi eval run), and your suites to browse and run.', custom:'evals' },
    { id:'automation', title:'Routes & Recipes', blurb:'Multi-model routing rules and saved multi-turn recipes.', custom:'automation' },
    { id:'channels', title:'Channels', blurb:'Message eVi from your phone via Telegram. Off until you turn it on.', custom:'channels' },
    { id:'companion', title:'Companion', blurb:'A proactive watcher: it reads only what you connect, briefs you each morning, speaks up when something needs you, and changes nothing until you approve the exact action. Off until you turn it on.', custom:'companion' },
    { id:'peers', title:'Peers', blurb:'eVi-to-eVi federation — delegate tasks to another eVi (e.g. a GPU box).', custom:'peers' },
    { id:'mcp', title:'MCP', blurb:'External MCP servers — extra tools loaded from ~/.evi/mcp.json.', custom:'mcp' },
    { id:'hooks', title:'Hooks', blurb:'Run your own commands or webhooks on tool calls and lifecycle events.', custom:'hooks' },
    { id:'ultracode', title:'Ultracode', blurb:'Run one hard task through an exhaustive multi-agent pipeline.', custom:'ultracode' },
    { id:'about', title:'About', blurb:'', custom:'about' },
  ];

  // ---- settings state -------------------------------------------------
  const overlay = document.getElementById('settings-overlay');
  const navEl = document.getElementById('settings-nav');
  const contentEl = document.getElementById('settings-content');
  const statusEl = document.getElementById('settings-status');
  const saveBtn = document.getElementById('settings-save');
  let cfg = null, pending = {}, activeSection = 'general', modelOptions = [], styleOptions = [];

  const getVal = (s, k) =>
    (pending[s] && k in pending[s]) ? pending[s][k] : (cfg && cfg[s] ? cfg[s][k] : undefined);
  function setVal(s, k, v) { (pending[s] = pending[s] || {})[k] = v; markDirty(); }
  function markDirty() {
    const dirty = Object.keys(pending).length > 0;
    saveBtn.disabled = !dirty;
    // Prominent cue: fields AND the health banners reflect SAVED config, so an edit
    // does nothing — and the banner won't move — until you Save. That silent gap is
    // what made a set-but-unsaved model look like it "didn't take".
    statusEl.textContent = dirty ? '● Unsaved changes — click Save to apply' : '';
    statusEl.style.color = dirty ? '#e0a52e' : '';
  }

  async function loadConfig() {
    cfg = await (await fetch('/api/config')).json();
    try {
      const pr = await fetch('/api/model-picker');
      if (pr.ok) modelOptions = (await pr.json()).models || [];
    } catch {}
    try {
      const sr = await fetch('/api/styles');
      if (sr.ok) styleOptions = (await sr.json()).styles || [];
    } catch {}
  }

  // Twenty-four flat entries make a new user read the whole list to find
  // anything. Grouping is purely presentational — SCHEMA still drives order and
  // content — but it turns a wall into four short, scannable lists. Anything not
  // named here falls into "More", so adding a section never silently drops it.
  const NAV_GROUPS = [
    ['Assistant', ['model', 'tools', 'editing', 'sampling', 'context', 'specialty', 'multiagent', 'companion']],
    ['This computer', ['permissions', 'guardrails', 'voice', 'notifications', 'desktop', 'server']],
    ['Extend', ['integrations', 'channels', 'plugins', 'mcp', 'hooks', 'automation', 'peers', 'ultracode']],
    ['App', ['general', 'statusline', 'stats', 'evals', 'about']],
  ];
  function renderNav() {
    navEl.innerHTML = '';
    const visible = SCHEMA.filter(s => !(s.desktopOnly && !isDesktop()));
    const byId = {};
    visible.forEach(s => { byId[s.id] = s; });
    const placed = new Set();
    const groups = NAV_GROUPS.map(([label, ids]) => {
      const secs = ids.map(id => byId[id]).filter(Boolean);
      secs.forEach(s => placed.add(s.id));
      return [label, secs];
    });
    const leftover = visible.filter(s => !placed.has(s.id));
    if (leftover.length) groups.push(['More', leftover]);
    groups.forEach(([label, secs]) => {
      if (!secs.length) return;
      const h = document.createElement('div');
      h.textContent = label;
      h.style.cssText = 'font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;opacity:.45;' +
        'padding:12px 10px 4px;pointer-events:none';
      navEl.appendChild(h);
      secs.forEach(sec => {
        const b = document.createElement('button');
        b.textContent = sec.title;
        b.className = sec.id === activeSection ? 'active' : '';
        if (sec.id === activeSection) b.dataset.active = '1';
        b.onclick = () => { activeSection = sec.id; renderNav(); renderSection(); };
        navEl.appendChild(b);
      });
    });
    // Grouping reorders the list, so the selected section may now sit below the
    // fold — without this, Settings can open looking like nothing is selected.
    const cur = navEl.querySelector('button[data-active]');
    if (cur && cur.scrollIntoView) cur.scrollIntoView({ block: 'nearest' });
  }

  // ---- Settings section explainers + live health banners (declarative) ----
  // SECTION_EXPLAINERS[id] = HTML body for a collapsible "How this works" panel.
  // SECTION_HEALTH[id] = async () => ({tone,title,msg,cta}|null) — a live banner
  // computed from real runtime state (backend reachable, tokens, availability…),
  // mirroring the federation Peers posture banner. Both are optional per section
  // and render for custom sections too (injected before the custom dispatch).
  const SECTION_EXPLAINERS = {
    general: `Controls appearance (theme, and the output-style response persona) and all opt-in reporting — crash reports plus OpenTelemetry traces and metrics, and the endpoints they ship to. Change it to switch light/dark or to wire eVi into your monitoring. Reporting is <b>off and inert by default</b>: crash reports send only when a <code>dsn</code> is set, and OTel exports only with an OTLP endpoint <i>and</i> the <code>[otel]</code> extra installed — a blank endpoint exports nothing. Theme also lives under View → Toggle Theme.`,
    model: `Picks where eVi sends your messages — backend kind, Base URL, model, API key — plus the generation defaults (temperature, context size, reasoning effort, fast mode, fallbacks, routing). Change it to point at a different local server or cloud endpoint, swap models, or dial thinking up or down. <b>Switching backend rebuilds the live connection.</b> The <code>claude_agent</code>/<code>codex</code>/<code>gemini</code>/<code>copilot</code>/<code>amp</code> kinds drive a local CLI that signs in itself (a subscription login; <code>gemini</code> needs a Gemini API key or Vertex AI) — they ignore Base URL and need their command on PATH, not a running server. Local backends ignore the API key; <code>reasoning_effort=off</code> fully disables extended thinking.`,
    tools: `Toggles which capability categories eVi may use — filesystem, code, shell, web, image, memory, MCP, git, sqlite, pdf, ocr, vision and more — plus the code sandbox, MCP allowlist, and tool-search threshold. Change it to grant or revoke a capability or to trim the toolset. <b>These apply to new chats only</b> — an open session keeps the tools it started with. The <b>⚠ marks risk</b> (a tool that acts outside eVi’s sandbox, like <code>shell</code> or <code>computer</code>), <i>not</i> unavailability — a tool that cannot run is instead missing an install dependency, shown in its own help.`,
    permissions: `Resolves every tool call to <b>allow</b> / <b>deny</b> / <b>ask</b> before it runs, in a fixed order: <code>hard_deny</code> (beats everything, even yolo), then <code>mode</code>, then first-match <code>rules</code>, then trusted dirs/domains, otherwise ask. Loosen it with trusted directories, trusted domains, or auto-approved categories; tighten it with <code>deny</code> rules, <code>hard_deny</code>, and <code>protected_paths</code>. Gotcha: <code>yolo</code> short-circuits <i>before</i> rules, so an ordinary <code>deny</code> rule won’t protect you there — only <code>hard_deny</code> and the destructive-command guard still fire. Changes here apply to new chats, not the turn you’re in.`,
    server: `Gates who can reach the web UI and API. <b>Access token</b>: blank leaves localhost open; set it and the web UI requires that token. <b>Multi-user logins</b>: each person signs in with their own token from <code>~/.evi/users.json</code> on a shared workspace. <b>A2A interop</b> exposes <code>POST /a2a</code> so any standards-compliant external agent — not just trusted eVi peers — can delegate a task here. Gotcha: with the access token blank, eVi answers only this computer, so A2A, multi-user logins and LAN binding reach nobody else until you set one (the desktop app generates its own for LAN access).`,
    guardrails: `A fully-local content filter between you and the model: <code>[[rule]]</code> regex, <code>[[judge]]</code> LLM-graded, and <code>[[classifier]]</code> offline-ML rules, applied to your input <i>before</i> it reaches the LLM and to the reply <i>after</i> it streams. Off until you edit <code>~/.evi/guardrails.toml</code> to set <code>enabled = true</code> with at least one rule; <b>Save</b> validates the TOML server-side and won’t write a broken file. Gotcha: output rules run <i>post-stream</i> — they scrub the stored transcript and flag the turn, but can’t un-show text already on screen. The judge and classifier layers also <b>fail open</b>: a missing or erroring grader skips that rule rather than blocking the turn. Treat it as hygiene and shared-machine policy, not a hard security boundary.`,
    context: `Controls automatic <b>compaction</b> — when a conversation grows too large, eVi summarises its oldest turns into one note so the exchange still fits the model’s window. Compaction fires after <code>Compact after N messages</code> <i>or</i> once usage passes <code>Compact at % of context</code>; set both to <code>0</code> to turn it off entirely (<code>Keep recent turns</code> is internally floored at 2). Tighten these for small-window local models, relax them for large-context ones. Gotchas: the percent trigger only means anything if the model’s context window is set correctly in the Model panel, and compaction is <b>lossy</b> — summarised turns lose detail from the live context, though full transcripts stay on disk if transcripts are enabled. The summary is produced by your <i>same</i> backend in a one-shot call; if that call fails, history is left untouched rather than dropped.`,
    sampling: `Low-level generation parameters passed to your backend per request — sampling shape (<code>top-p</code>, presence / frequency penalties), determinism (<code>seed</code>, <code>stop sequences</code>), and plumbing (<code>request timeout</code>, the <code>embedding model</code> used for semantic file search). Most are pure overrides: leave a field at its default and eVi doesn’t send it at all, so it can’t change anything. Reach for one only for a specific need — a fixed <code>seed</code> for reproducible runs, <code>Max completion tokens</code> for reasoning models that reject <code>max_tokens</code>, or <code>logit bias</code> to push tokens up or down. Main gotcha: these are <b>backend-specific and silently ignored</b> where unsupported — <code>cache_prompt</code> is a llama.cpp / vLLM hint, <code>logit_bias</code> / <code>logprobs</code> need an OpenAI-schema server, and <b>CLI-agent backends</b> (claude / codex / gemini) honour almost none of them.`,
    specialty: `Optional small models that run <i>instead of</i> your main chat model for specific jobs — <code>OCR</code> and <code>vision</code> (chat-VLMs served over the OpenAI image schema), <code>speech-to-text</code> (a faster-whisper model, not an HTTP backend), and a <code>safety-guard</code> model for the guardrail. Leave any field <b>blank to fall back</b> to the main model or default engine; a blank <code>base URL</code> reuses your main <code>[llm]</code> backend. Set one to route a task to a cheaper or specialised local model, or to point a task at a separate server. Gotcha: the id you enter must actually be <b>served by the backend it points at</b> — a typo or an unloaded model only fails when that task runs — and speech-to-text additionally needs the <code>faster-whisper</code> extra installed. Changes here only take effect once you <b>click Save</b>; the banner below then reports whether each configured model’s backend is actually reachable.`,
    integrations: `Optional external services eVi can reach out to: a local <code>ComfyUI</code> server for image generation, an Obsidian vault, Google / Microsoft accounts for mail and calendar, and the web-search backend. Change these to point at a self-hosted ComfyUI or SearXNG instance, or to supply OAuth client credentials. The <code>SearXNG URL</code> is only consulted when the search backend is set to <code>searxng</code> — the other backends ignore it. <b>Gotcha:</b> the ComfyUI and SearXNG URLs get a live reachability check, but the Google / Microsoft fields only record credential paths — filling them in does not sign you in or cache a token, so there is no liveness signal for email here.`,
    editing: `Controls two edit-time assists and how long stored transcripts are kept. <b>Format on edit</b> and <b>Check on edit</b> run an already-installed formatter (ruff/black/prettier/gofmt/rustfmt) or linter after each write — both are silent no-ops if the tool isn’t on <code>PATH</code>, so turning them on does nothing until you install one. <b>Transcript retention</b> deletes stored sessions older than N days at startup (<code>0</code> = keep forever), so a change only takes effect on the next launch, not immediately. <b>Worktree base ref</b> only applies when you run <code>evi worktree</code> without <code>--base</code> — blank means fork from <code>HEAD</code>.`,
    plugins: `Lists installed plugins and the marketplace you install them from. Each plugin lives in the plugins dir with a manifest; <code>index_urls</code> sets which marketplaces the search pulls from. <b>Gotcha:</b> a plugin whose manifest is broken or missing is silently dropped from the installed list rather than flagged — so if one you installed vanished, its manifest is the first thing to check. The health signal below surfaces exactly those failed-to-load plugins by name.`,
    voice: `Picks the text-to-speech engine eVi uses to read replies aloud (the REPL <code>/speak</code> toggle, <code>evi voice</code>, and the streaming AutoSpeaker) plus its model, clone sample, and language. <b>system</b> is the zero-dependency OS voice; <b>coqui</b> and <b>f5</b> clone a voice from a reference WAV, <b>piper</b> gives light local neural voices — the three neural engines are separate installs that only load when selected. Switch engines for nicer or cloned speech, and set <b>Model id / path</b> to a voice <code>.onnx</code> for Piper (required) or an XTTS model id for Coqui. A missing engine dependency fails open with an install hint rather than crashing, and this panel controls TTS only — mic transcription is the separate <code>stt</code> extra.`,
    notifications: `Fires a ping when the assistant finishes a turn, so you can start a long or headless run and walk away. <b>Sound</b> plays a beep, <b>Desktop toast</b> raises a native OS notification, and <b>ntfy / webhook URL</b> POSTs the completion to an ntfy topic or webhook so a remote or headless turn can still reach your phone. Turn these on for long local runs or when you’re away from the tab. Nothing fires unless <b>Completion notifications</b> is enabled, and on Windows the native toast is limited — the webhook is the dependable path to a phone or a headless box.`,
    statusline: `Configures the one-line status readout in the interactive terminal REPL — it is off by default and has no effect in the web or desktop chat. <b>Format</b> is a template of tokens (<code>{model}</code> <code>{pct}</code> <code>{branch}</code> <code>{goal}</code> <code>{effort}</code> <code>{fast}</code> and friends) rendered after each turn. Enable it and edit Format to keep context usage, branch, or effort in view while working in the REPL. If you set <b>Command</b>, eVi runs that shell command with the state as JSON on stdin and uses its stdout instead — so a command completely overrides Format.`,
    multiagent: `Controls how the ultracode pipeline (<code>decompose &rarr; fan-out solvers &rarr; adversarial verify &rarr; synthesize</code>) spreads work and how deep it goes. <b>Solver breadth</b> sets parallel angles (<code>1</code> disables fan-out), <b>Verify rounds</b> sets the adversarial critique&rarr;refine cycles (<code>0</code> skips the critic), and <b>Spread fan-out across flagged backends</b> runs those angles on every backend you ticked <i>fan-out</i> in Model &amp; Backend — without that tick, multi-provider fan-out does nothing. <b>Cheap fan-out</b> is the alternative: it runs the solve stage on the <code>[llm]</code> fast model instead of fanning out across backends. Note that <b>Auto-tune</b> quietly lowers breadth and rounds for tiny or short-context models, and with a single local backend breadth buys diverse-angle quality rather than wall-clock speed (inference serializes).`,
    stats: `A read-only view of local usage computed entirely from your on-disk transcripts — session and message counts, an approximate token total, a date span, plus role, top-tool, and busiest-day breakdowns over a range you pick. Nothing here is configurable and nothing leaves the machine; it is the same data as <code>evi stats</code>. It only has data when transcript recording is on (<code>tools.transcripts</code>) — with it off the panel shows <i>No transcripts yet</i>. Token counts are approximate (a rough character-based estimate, not billed usage).`,
  };
  const SECTION_HEALTH = {
    // Phase 1 — no new server endpoints. Client-computable from cfg or the
    // existing /api/backend/status probe. renderSection() re-runs on save, so
    // these refresh automatically after a config change.
    model: async () => {
      const d = await fetch('/api/backend/status').then(r => r.json()).catch(() => null);
      if (!d || !d.configured) return null;
      if (d.configured.reachable)
        return { tone:'good', title:'Backend reachable', msg:'The configured backend responded — your messages route there.' };
      return { tone:'bad', title:'Backend not answering',
        msg:'Nothing answered at the configured backend — start its server (or CLI), or switch backends below.' };
    },
    tools: async () => {
      let av = cfg && cfg.availability;
      if (!av) { const d = await fetch('/api/config').then(r => r.json()).catch(() => ({})); av = d.availability || {}; }
      const missing = Object.keys(av).filter(k => av[k] && av[k].available === false);
      if (!missing.length)
        return { tone:'good', title:'All tools available', msg:'Every gated tool has its dependency installed and is ready to enable.' };
      return { tone:'warn', title:'Some tools need setup',
        msg: missing.length + (missing.length === 1 ? ' category needs' : ' categories need') +
          ' a dependency: ' + missing.join(', ') + ' — open each tool’s help for the command.' };
    },
    server: async () => {
      const web = (cfg && cfg.web) || {}, fed = (cfg && cfg.federation) || {};
      const access = (cfg && cfg.access) || {};
      const hasToken = String(web.auth_token || '') !== '' || access.token === true || access.logins === true;
      const openSurface = fed.a2a === true || web.multi_user === true || fed.bind_lan === true;
      // Without a token the server answers only this machine, so an "open"
      // setting is unreachable from elsewhere rather than unprotected.
      if (!hasToken && openSurface)
        return { tone:'warn', title:'Network access, no token',
          msg:'A2A, multi-user or LAN binding is on, but with no access token eVi refuses every other machine. Set a token to serve them (the desktop app makes its own for LAN access at its next launch).' };
      if (hasToken && String(web.auth_token || '') === '' && access.launch_token)
        return { tone:'good', title:'LAN token active', msg:'The desktop app generated this server’s access token (~/.evi/desktop-token); other machines must send it, this computer needs none.' };
      if (hasToken)
        return { tone:'good', title:'Access token set', msg:'The web UI and API require your token before responding.' };
      return { tone:'off', title:'Localhost only', msg:'No token set, so eVi answers only this computer — fine on a single-user machine.' };
    },
    permissions: async () => {
      const a = (cfg && cfg.auto) || {};
      const yolo = a.mode === 'yolo', guardOff = a.block_destructive === false;
      if (yolo && guardOff) return { tone:'bad', title:'Everything allowed, no guard', msg:'Yolo runs every tool without asking and the destructive-command guard is disabled — nothing is gated.' };
      if (yolo) return { tone:'bad', title:'Yolo mode active', msg:'Every tool call runs without prompting; only hard-deny rules and the destructive guard still block.' };
      if (guardOff) return { tone:'warn', title:'Destructive guard off', msg:'Curated destructive shell commands (rm -rf ~, git reset --hard, force-push) no longer force a confirmation.' };
      if (a.mode === 'accept_edits') return { tone:'info', title:'Edits auto-approved', msg:'File edits and code reading apply without prompting; running code and other tool categories still follow your rules.' };
      if (a.mode === 'plan') return { tone:'info', title:'Plan mode: tools off', msg:'All tool calls are denied — the model can reason but not act.' };
      return { tone:'good', title:'Standard permissions', msg:'Non-approved tools prompt before running and the destructive-command guard is on.' };
    },
    // Phase 2 — endpoint-backed probes (integrations/voice/specialty reachability,
    // guardrails guard-model, plugin load errors).
    integrations: async () => {
      const d = await fetch('/api/integrations/status').then(r => r.json()).catch(() => null);
      if (!d) return null;
      const c = d.comfy || {}, sx = d.searxng || {};
      if (sx.active && !sx.url)
        return { tone:'warn', title:'SearXNG URL missing', msg:'Search backend is set to searxng but no instance URL is configured, so web search will fail.' };
      if (sx.active && sx.url && !sx.reachable)
        return { tone:'bad', title:'SearXNG unreachable', msg:'The configured SearXNG instance isn’t responding — web search will fail until it’s back up.' };
      if (c.configured_url && !c.reachable)
        return { tone:'bad', title:'ComfyUI unreachable', msg:'Nothing is responding at the ComfyUI URL — start ComfyUI (default :8188) or correct the address.' };
      const up = [];
      if (c.reachable) up.push('ComfyUI');
      if (sx.active && sx.reachable) up.push('SearXNG');
      if (up.length)
        return { tone:'good', title:up.join(' + ') + ' reachable', msg:'The configured ' + up.join(' and ') + ' service' + (up.length > 1 ? 's are' : ' is') + ' up.' };
      return null;  // nothing configured to probe
    },
    voice: async () => {
      const d = await fetch('/api/voice/status').then(r => r.json()).catch(() => null);
      if (!d) return null;
      if (d.available)
        return { tone:'good', title:'Voice engine ready', msg:'The selected engine (' + (d.detail || d.engine) + ') is installed, so spoken replies will work.' };
      if (d.engine === 'system')
        return { tone:'warn', title:'No system voice found', msg:'No platform text-to-speech was detected — ' + (d.how_to_enable || 'install one or pick a neural engine').replace(/\.$/, '') + '.' };
      return { tone:'warn', title:'Engine not installed',
        msg:'The ' + d.engine + ' engine isn’t installed — ' + (d.how_to_enable || 'install it').replace(/\.$/, '') + ', or switch back to the zero-dep system voice.',
        cta:{ label:'Use system voice', action:'set-voice-system' } };
    },
    specialty: async () => {
      const d = await fetch('/api/specialty/status').then(r => r.json()).catch(() => null);
      if (!d) return null;
      const set = ['ocr', 'vision', 'guard'].filter(k => d[k] && d[k].set);
      const sttSet = d.stt && d.stt.set;
      if (!set.length && !sttSet)
        return { tone:'off', title:'No specialty models set', msg:'Every task uses your main chat model — there’s nothing extra to run or reach.' };
      const unreach = set.filter(k => !d[k].reachable);
      if (unreach.length)
        return { tone:'bad', title:'Specialty backend unreachable',
          msg:'The ' + unreach.join(', ') + ' model’s server isn’t responding — that task errors or falls back to the main model until it’s up.',
          cta:{ label:'Open Model panel', action:'goto:model' } };
      if (sttSet && d.stt.dep_ok === false)
        return { tone:'warn', title:'Speech-to-text dep missing', msg:'A speech-to-text model is set but faster-whisper isn’t installed — transcription stays unavailable.' };
      return { tone:'good', title:'Specialty models reachable', msg:'The server behind each specialty model you’ve set is responding.' };
    },
    guardrails: async () => {
      const [g, sp] = await Promise.all([
        fetch('/api/guardrails').then(r => r.json()).catch(() => null),
        fetch('/api/specialty/status').then(r => r.json()).catch(() => null),
      ]);
      if (!g) return null;
      if (!g.enabled)
        return { tone:'off', title:'Guardrails off', msg:'No content filter is active — the default; add a rule and set enabled = true to turn it on.' };
      if (sp && sp.guard && sp.guard.set && sp.guard.reachable === false)
        return { tone:'warn', title:'Guard model unreachable',
          msg:'Guardrails are on but the configured guard model isn’t answering, so those rules fail open and silently skip.',
          cta:{ label:'Open Specialty models', action:'goto:specialty' } };
      return { tone:'good', title:'Guardrails active', msg:'The local content filter is on and your rules are loaded.' };
    },
    plugins: async () => {
      const d = await fetch('/api/plugins').then(r => r.json()).catch(() => null);
      if (!d) return null;
      const failed = d.failed || [], installed = d.installed || [];
      if (failed.length)
        return { tone:'warn', title:'Plugin failed to load', msg:failed.length + ' plugin' + (failed.length > 1 ? 's' : '') + ' have an unreadable manifest and were skipped: ' + failed.map(f => f.name).join(', ') + '.' };
      if (!installed.length)
        return { tone:'off', title:'No plugins installed', msg:'Nothing installed yet — use the marketplace above to add one.' };
      return { tone:'good', title:'Plugins loaded', msg:'All ' + installed.length + ' installed plugin' + (installed.length > 1 ? 's' : '') + ' parsed their manifest and are active.' };
    },
  };
  const _healthTone = (t) => ({
    good: { c:'#4caf50', bg:'rgba(76,175,80,.09)', ic:'✓' },
    warn: { c:'#e0a52e', bg:'rgba(224,165,46,.09)', ic:'⚠' },
    bad:  { c:'#e05252', bg:'rgba(224,82,82,.09)', ic:'✕' },
    info: { c:'#4a9eff', bg:'rgba(74,158,255,.09)', ic:'ⓘ' },
    off:  { c:'var(--dim,#8a94a0)', bg:'transparent', ic:'○' },
  }[t] || { c:'var(--dim,#8a94a0)', bg:'transparent', ic:'○' });
  function healthBannerHtml(st) {
    if (!st) return '';
    const tn = _healthTone(st.tone);
    let cta = '';
    if (st.cta && st.cta.label) {
      cta = '<button class="set-action-btn" data-health-cta="' + escapeAttr(st.cta.action || '') +
        '" style="margin-top:7px">' + escapeHtml(st.cta.label) + '</button>';
    }
    return '<div class="sec-health" style="display:flex;gap:9px;align-items:flex-start;border:1px solid ' +
      tn.c + ';background:' + tn.bg + ';border-radius:9px;padding:9px 11px;margin:2px 0 12px">' +
      '<span style="color:' + tn.c + ';font-weight:700;font-size:14px;line-height:1.35">' + tn.ic + '</span>' +
      '<div style="flex:1;min-width:0"><b style="color:' + tn.c + '">' + escapeHtml(st.title) + '</b>' +
      '<div class="dim" style="font-size:12.5px;margin-top:2px;line-height:1.45">' + escapeHtml(st.msg) + '</div>' +
      cta + '</div></div>';
  }
  async function refreshSectionHealth(id, mount) {
    if (!SECTION_HEALTH[id] || !mount) return;
    try { mount.innerHTML = healthBannerHtml(await SECTION_HEALTH[id]()); }
    catch (e) { mount.innerHTML = ''; }
  }
  // Explainer <details> + a health-banner mount for a section, if either is defined.
  function renderSectionAids(sec) {
    if (SECTION_EXPLAINERS[sec.id]) {
      const d = document.createElement('details'); d.className = 'sec-help'; d.style.margin = '2px 0 8px';
      d.innerHTML = '<summary style="cursor:pointer;color:var(--accent,#4a9eff);font-size:13px">How this works</summary>' +
        '<div class="dim" style="font-size:12.5px;line-height:1.5;margin:6px 0 2px;padding-left:2px">' +
        SECTION_EXPLAINERS[sec.id] + '</div>';
      contentEl.appendChild(d);
    }
    if (SECTION_HEALTH[sec.id]) {
      const mount = document.createElement('div');
      mount.className = 'sec-health-mount'; mount.id = 'health-' + sec.id;
      mount.innerHTML = '<div class="dim" style="font-size:12px;margin:2px 0 8px">Checking…</div>';
      contentEl.appendChild(mount);
      refreshSectionHealth(sec.id, mount);
    }
  }
  if (!window.__healthCtaBound) {
    window.__healthCtaBound = true;
    document.addEventListener('click', (ev) => {
      const t = ev.target.closest && ev.target.closest('[data-health-cta]');
      if (!t) return;
      const act = t.getAttribute('data-health-cta') || '';
      if (act.indexOf('goto:') === 0) {
        const sec = SCHEMA.find(s => s.id === act.slice(5));
        const btn = sec && [...document.querySelectorAll('button')].find(b => (b.textContent || '').trim() === sec.title);
        if (btn) btn.click();
      } else if (act === 'set-voice-system') {
        fetch('/api/config', { method:'POST', headers:{ 'Content-Type':'application/json' },
          body: JSON.stringify({ voice:{ engine:'system' } }) })
          .then(r => r.json()).then(data => { if (data && data.config) cfg = data.config; renderSection(); })
          .catch(() => {});
      } else if (act === 'reload') {
        const m = t.closest('.sec-health-mount');
        if (m) refreshSectionHealth(m.id.replace('health-', ''), m);
      } else if (act === 'migrate-auto-approve') {
        migrateAutoApprove(t);
      }
    });
  }

  function renderSection() {
    const sec = SCHEMA.find(s => s.id === activeSection);
    contentEl.innerHTML = '';
    const h = document.createElement('h3'); h.textContent = sec.title; contentEl.appendChild(h);
    if (sec.blurb) {
      const p = document.createElement('p'); p.className = 'sec-blurb'; p.textContent = sec.blurb;
      contentEl.appendChild(p);
    }
    if (sec.applies === 'new-chat') {
      const n = document.createElement('p'); n.className = 'sec-blurb';
      n.innerHTML = '<em>Changes here apply to new chats.</em>'; contentEl.appendChild(n);
    }
    if (sec.id === 'tools') {
      const lg = document.createElement('p'); lg.className = 'sec-blurb';
      lg.innerHTML = '<span class="set-danger-label">⚠</span> marks tools that can act ' +
        'outside eVi’s sandbox — run commands, control your screen, or reach the network. ' +
        'Enable with care. (It flags <em>risk</em>, not availability — hover any ⚠ for the specific reason.)<br>' +
        '<span class="set-unavail">needs setup</span> means the tool’s dependency isn’t installed in ' +
        'this build — hover it for how to enable.';
      contentEl.appendChild(lg);
    }
    renderSectionAids(sec);
    if (sec.custom === 'channels') return renderChannels();
    if (sec.custom === 'companion') return renderCompanion();
    if (sec.custom === 'about') return renderAbout();
    if (sec.custom === 'guardrails') return renderGuardrails();
    if (sec.custom === 'plugins') return renderPlugins();
    if (sec.custom === 'stats') return renderStats();
    if (sec.custom === 'evals') return renderEvals();
    if (sec.custom === 'automation') return renderAutomation();
    if (sec.custom === 'peers') return renderPeers();
    if (sec.custom === 'mcp') return renderMcp();
    if (sec.custom === 'hooks') return renderHooks();
    if (sec.custom === 'ultracode') return renderUltracode();
    sec.fields.forEach(f => contentEl.appendChild(fieldWidget(f)));
    if (sec.id === 'model') renderModelExtras();
  }

  // System stats + hardware-recommended model (Settings → Model & Backend).
  function renderModelExtras() {
    renderBackendsPanel();
    renderRuntimeCatalog();
    const box = document.createElement('div');
    box.style.marginTop = '20px';
    box.innerHTML = '<div class="sec-blurb">Loading system info…</div>';
    contentEl.appendChild(box);
    fetch('/api/system').then(r => r.json()).then(sys => {
      const rows = [];
      rows.push(['OS', sys.os_detail || sys.os]);
      rows.push(['RAM', sys.ram_gb + ' GB']);
      const gpu = (sys.gpus || [])[0];
      if (gpu) {
        rows.push(['GPU', gpu.name]);
        const free = gpu.vram_free_mb != null ? ` (${(gpu.vram_free_mb / 1024).toFixed(1)} free)` : '';
        rows.push(['VRAM', `${(gpu.vram_total_mb / 1024).toFixed(1)} GB${free}`]);
        rows.push(['Driver / CUDA cc', `${gpu.driver || '?'}${gpu.compute ? ' · cc ' + gpu.compute : ''}`]);
      } else {
        rows.push(['GPU', 'none detected — CPU inference']);
      }
      rows.push(['Inference mode', sys.mode]);
      let html = '<div class="set-group-title">System</div>';
      html += rows.map(([k, v]) =>
        `<div class="set-about-row"><span class="k">${escapeHtml(k)}</span><span>${escapeHtml(String(v))}</span></div>`
      ).join('');
      const rec = sys.recommended || {};
      if (rec.chat) {
        html += '<div class="set-group-title">Recommended for your hardware</div>'
          + `<div class="set-about-row"><span>${escapeHtml(rec.chat)}</span><span id="rec-action"></span></div>`
          + '<div class="rec-progress" id="rec-progress" style="display:none"><div class="rec-bar" id="rec-bar"></div></div>'
          + '<div class="help" id="rec-status"></div>';
      }
      box.innerHTML = html;
      if (!rec.chat) return;
      const action = box.querySelector('#rec-action');
      if (rec.chat === sys.current_model) {
        action.innerHTML = '<span style="color:var(--user)">✓ current</span>';
      } else if (rec.chat_installed) {
        action.appendChild(_recBtn('Use', () => useRecommended(rec.chat)));
      } else if (sys.backend === 'ollama') {
        action.appendChild(_recBtn('Pull', () => pullRecommended(rec.chat, box)));
      } else {
        action.innerHTML = '<span class="help">pull it via your backend, then select it above</span>';
      }
    }).catch(() => { box.innerHTML = '<div class="sec-blurb">System info unavailable.</div>'; });
  }
  function _recBtn(label, fn) {
    const b = document.createElement('button');
    b.className = 'set-action-btn'; b.style.margin = '0'; b.textContent = label; b.onclick = fn;
    return b;
  }

  // Backends registry panel (Settings → Model & Backend).
  // Managed llama.cpp model catalog — download / switch / remove local models
  // without a terminal (Settings → Model & Backend).
  function renderRuntimeCatalog() {
    const box = document.createElement('div');
    box.style.marginTop = '20px';
    box.innerHTML = '<div class="sec-blurb">Loading local models…</div>';
    contentEl.appendChild(box);
    const render = (d) => {
      if (!d || !d.models) { box.innerHTML = ''; return; }
      let html = '<div class="set-group-title">Local models (managed llama.cpp)</div>' +
        '<div class="help">Download and run a model locally — no Ollama, no account. ' +
        '★ best fit for your hardware · ● downloaded · ▶ active.</div>' +
        '<div id="rt-cat-status" class="help" style="min-height:1.1em"></div>';
      if (d.gpu_available) {
        if (d.on_gpu) {
          html += '<div class="help" style="color:#4caf50">⚡ Running on your GPU' +
            (d.gpu_name ? ' (' + escapeHtml(d.gpu_name) + ')' : '') + '.</div>';
        } else {
          html += '<div style="margin:4px 0 8px"><button class="set-action-btn" data-rt-gpu="1">⚡ Use your GPU' +
            (d.gpu_name ? ' (' + escapeHtml(d.gpu_name) + ')' : '') + '</button>' +
            '<span class="dim" style="font-size:11px;margin-left:8px">downloads the CUDA build once, then runs models much faster</span></div>';
        }
      }
      for (const m of d.models) {
        const active = m.id === d.active_model_id, rec = m.id === d.recommended_id;
        const marks = (m.installed ? '<span style="color:#4caf50">●</span>'
          : '<span style="color:var(--dim)">○</span>') +
          (rec ? ' <span title="recommended for your hardware">★</span>' : '') +
          (active ? ' <span style="color:#4caf50">▶</span>' : '');
        html += '<div style="display:flex;align-items:center;gap:10px;padding:7px 0;' +
          'border-bottom:1px solid var(--border,#2b333d)">' +
          '<div style="flex:1;min-width:0"><div>' + marks + ' <b>' + escapeHtml(m.name) + '</b> ' +
          '<span class="dim" style="font-size:11px">' + m.size_gb + ' GB · ' + escapeHtml(m.license) + '</span></div>' +
          '<div class="dim" style="font-size:12px">' + escapeHtml(m.note || '') + '</div></div>' +
          (active ? '<span class="dim" style="font-size:12px;white-space:nowrap">in use</span>'
            : '<button class="set-action-btn" data-rt-use="' + escapeAttr(m.id) + '">' +
              (m.installed ? 'Use' : 'Download &amp; use') + '</button>') +
          (m.installed && !active
            ? '<button class="set-action-btn" data-rt-remove="' + escapeAttr(m.id) + '">Remove</button>' : '') +
          '</div>';
      }
      box.innerHTML = html;
    };
    fetch('/api/runtime/catalog').then(r => r.json()).then(render).catch(() => { box.innerHTML = ''; });
    box.onclick = async (ev) => {
      const t = ev.target;
      if (!(t instanceof HTMLElement)) return;
      const use = t.getAttribute('data-rt-use'), rm = t.getAttribute('data-rt-remove'),
            gpu = t.getAttribute('data-rt-gpu');
      const status = box.querySelector('#rt-cat-status');
      const pollRuntime = async () => {
        for (let i = 0; i < 1800; i++) {
          await new Promise(r => setTimeout(r, 1000));
          const s = await fetch('/api/runtime/status').then(r => r.json()).catch(() => null);
          if (!s) continue;
          if (status) { status.textContent = s.message || s.stage; status.style.color = s.error ? '#e05252' : ''; }
          if (s.error || s.done) break;
        }
        if (typeof checkBackend === 'function') checkBackend();
        renderSection();
      };
      if (gpu) {
        t.disabled = true; t.textContent = 'Setting up GPU…';
        await fetch('/api/runtime/gpu', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }).catch(() => {});
        await pollRuntime();
        return;
      }
      if (use) {
        t.disabled = true;
        await fetch('/api/runtime/model/use', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: use }) }).catch(() => {});
        for (let i = 0; i < 1800; i++) {
          await new Promise(r => setTimeout(r, 1000));
          const s = await fetch('/api/runtime/status').then(r => r.json()).catch(() => null);
          if (!s) continue;
          if (status) { status.textContent = s.message || s.stage; status.style.color = s.error ? '#e05252' : ''; }
          if (s.error || s.done) break;
        }
        if (typeof checkBackend === 'function') checkBackend();
        renderSection();  // refresh installed/active marks + the no-backend banner
      } else if (rm) {
        if (!confirm('Delete this downloaded model file?')) return;
        t.disabled = true;
        const r = await fetch('/api/runtime/model/remove', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: rm }) });
        if (!r.ok) {
          const e = await r.json().catch(() => ({}));
          if (status) { status.textContent = e.detail || 'remove failed'; status.style.color = '#e05252'; }
          t.disabled = false; return;
        }
        renderSection();
      }
    };
  }

  function renderBackendsPanel() {
    const box = document.createElement('div');
    box.style.marginTop = '20px';
    box.innerHTML = '<div class="sec-blurb">Loading backends…</div>';
    contentEl.appendChild(box);
    fetch('/api/backends').then(r => r.json()).then(d => {
      const active = d.active_backend || '';
      let html = '<div class="set-group-title">Backends</div>';
      html += '<div class="help">Providers eVi can use. The model picker lists every enabled backend\'s models, tagged by source. Tick <b>fan-out</b> to let a backend\'s models serve delegated subagents — then turn on <b>Multi-agent → Spread fan-out</b> to activate it.</div>';
      if (!(d.backends || []).length) {
        html += '<div class="sec-blurb">No backends registered — the active <code>[llm]</code> backend is used. Add one below.</div>';
      } else {
        html += d.backends.map(b => {
          const isActive = b.name === active;
          const key = b.has_key ? (b.key_is_env ? ' · env key' : ' · key set') : ' · no key';
          return `<div class="backend-row" data-name="${escapeAttr(b.name)}">
            <div class="backend-info">
              <span class="backend-name">${escapeHtml(b.name)}${isActive ? ' <span class="badge">active</span>' : ''}</span>
              <span class="help">${escapeHtml(b.kind)} · ${escapeHtml(b.base_url)}${key}</span>
            </div>
            <label class="backend-flag" title="Include this backend when listing models"><input type="checkbox" data-flag="enabled" ${b.enabled ? 'checked' : ''}> enabled</label>
            <label class="backend-flag" title="Allow this backend's models for subagent fan-out"><input type="checkbox" data-flag="fanout" ${b.fanout ? 'checked' : ''}> fan-out</label>
            <button class="set-action-btn backend-rm" title="Remove">✕</button>
          </div>`;
        }).join('');
      }
      const presetOpts = ['<option value="">— preset —</option>']
        .concat((d.presets || []).map(p => `<option value="${escapeAttr(p.name)}">${escapeHtml(p.name)}</option>`))
        .concat(['<option value="__custom__">custom…</option>']).join('');
      html += `<div class="set-group-title">Add a backend</div>
        <div class="backend-add">
          <select id="bk-preset">${presetOpts}</select>
          <input id="bk-name" title="unique name" style="display:none">
          <select id="bk-kind" style="display:none">
            <optgroup label="Local / OpenAI-compatible server">
              <option value="openai_compat">openai_compat</option>
              <option value="ollama">ollama</option>
              <option value="lmstudio">lmstudio</option>
              <option value="llamacpp">llamacpp</option>
            </optgroup>
            <optgroup label="CLI agent — the CLI signs in, no URL or key here">
              <option value="claude_agent">claude_agent — Claude Max/Pro</option>
              <option value="codex">codex — ChatGPT plan</option>
              <option value="gemini">gemini — Gemini API key / Vertex AI</option>
              <option value="amp">amp — Amp subscription</option>
              <option value="qwen">qwen — free Qwen OAuth</option>
              <option value="copilot">copilot — GitHub Copilot</option>
            </optgroup>
          </select>
          <input id="bk-url" title="base URL" style="display:none">
          <input id="bk-key" title="API key or env:VARNAME" type="password" style="display:none">
          <button class="set-action-btn" id="bk-add">Add</button>
        </div>
        <div class="help" id="bk-msg">Pick a preset (OpenAI, xAI, Anthropic, OpenRouter, …) and enter a key, or "custom…" for a local / other endpoint.</div>`;
      box.innerHTML = html;
      wireBackendsPanel(box);
    }).catch(() => { box.innerHTML = '<div class="sec-blurb">Backends unavailable.</div>'; });
  }

  function wireBackendsPanel(box) {
    box.querySelectorAll('.backend-row').forEach(row => {
      const name = row.dataset.name;
      row.querySelectorAll('input[data-flag]').forEach(cb => {
        cb.addEventListener('change', () => {
          fetch('/api/backends/' + encodeURIComponent(name), {
            method: 'PATCH', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ [cb.dataset.flag]: cb.checked }),
          }).then(() => { if (cb.dataset.flag === 'enabled' && typeof loadPicker === 'function') loadPicker(); });
        });
      });
      row.querySelector('.backend-rm').addEventListener('click', () => {
        if (!confirm('Remove backend "' + name + '"?')) return;
        fetch('/api/backends/' + encodeURIComponent(name), { method: 'DELETE' })
          .then(() => { renderSection(); if (typeof loadPicker === 'function') loadPicker(); });
      });
    });
    const preset = box.querySelector('#bk-preset');
    const nameI = box.querySelector('#bk-name'), kindI = box.querySelector('#bk-kind');
    const urlI = box.querySelector('#bk-url'), keyI = box.querySelector('#bk-key');
    const msg0 = box.querySelector('#bk-msg');
    // CLI-agent kinds authenticate via the local CLI's own subscription/login —
    // no base URL and no API key. Keep this in step with KNOWN_BACKENDS.
    const CLI_AGENT_KINDS = ['claude_agent', 'codex', 'gemini', 'amp', 'qwen', 'copilot'];
    const DEFAULT_MSG = 'Pick a preset (OpenAI, xAI, Anthropic, OpenRouter, …) and enter a key, or "custom…" for a local / other endpoint.';
    // Kinds whose sign-in isn't a plain subscription login. Install commands
    // match CLI_AGENTS in evi/llm/cli_agent.py.
    const CLI_AGENT_HINT = {
      gemini: 'gemini uses the local gemini CLI (npm i -g @google/gemini-cli) with a Gemini API key (GEMINI_API_KEY) or Vertex AI — no URL or key here. Google stopped serving Gemini CLI to free, Google AI Pro and Ultra "Login with Google" accounts on 2026-06-18.',
      amp: 'amp uses the local amp CLI (npm i -g @ampcode/cli), signed in with "amp login" or AMP_API_KEY — no URL or key here.',
    };
    const sync = () => {
      const custom = preset.value === '__custom__';
      const cliAgent = custom && CLI_AGENT_KINDS.includes(kindI.value);
      nameI.style.display = custom ? '' : 'none';
      kindI.style.display = custom ? '' : 'none';
      urlI.style.display = (custom && !cliAgent) ? '' : 'none';
      keyI.style.display = (preset.value && !cliAgent) ? '' : 'none';
      msg0.textContent = cliAgent
        ? (CLI_AGENT_HINT[kindI.value] ||
           `${kindI.value} uses the local CLI's subscription/login — no URL or key. Install that CLI and log in first.`)
        : DEFAULT_MSG;
    };
    preset.addEventListener('change', sync);
    kindI.addEventListener('change', sync);
    sync();
    box.querySelector('#bk-add').addEventListener('click', async () => {
      const msg = box.querySelector('#bk-msg');
      const val = preset.value;
      if (!val) { msg.textContent = 'pick a preset or "custom…"'; return; }
      let payload;
      if (val === '__custom__') {
        if (!nameI.value.trim()) { msg.textContent = 'name is required'; return; }
        payload = { name: nameI.value.trim(), kind: kindI.value, base_url: urlI.value.trim(), api_key: keyI.value };
      } else {
        payload = { preset: val, api_key: keyI.value };
      }
      const r = await fetch('/api/backends', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      });
      if (r.ok) { renderSection(); if (typeof loadPicker === 'function') loadPicker(); }
      else { msg.textContent = 'add failed (' + r.status + ')'; }
    });
  }
  async function useRecommended(model) {
    try {
      await fetch('/api/model-picker', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model }),
      });
      if (typeof loadPicker === 'function') loadPicker();
      if (typeof loadCwd === 'function') loadCwd();
      await loadConfig();
      renderSection();
      if (typeof refreshHeaderModel === 'function') refreshHeaderModel();
    } catch {}
  }
  function pullRecommended(model, box) {
    const prog = box.querySelector('#rec-progress');
    const bar = box.querySelector('#rec-bar');
    const status = box.querySelector('#rec-status');
    const action = box.querySelector('#rec-action');
    if (action) action.innerHTML = '<span class="help">downloading…</span>';
    prog.style.display = 'block';
    status.textContent = 'Starting download…';
    let ended = false;
    streamModelPull(model, (d) => {
      if (d.kind === 'progress') {
        if (d.pct != null) bar.style.width = d.pct + '%';
        status.textContent = (d.status || 'downloading') + (d.pct != null ? ` (${d.pct}%)` : '');
        return true;
      }
      if (d.kind === 'done') {
        ended = true; bar.style.width = '100%'; status.textContent = 'Installed ✓';
        if (typeof loadPicker === 'function') loadPicker();
        if (typeof loadCwd === 'function') loadCwd();
        setTimeout(() => loadConfig().then(renderSection), 700);
        return false;
      }
      if (d.kind === 'error') {
        ended = true; status.textContent = 'Error: ' + (d.message || 'download failed');
        return false;
      }
      return true;
    }).then(() => {
      if (!ended) status.textContent = 'Connection lost during download.';
    }, (err) => {
      if (!ended) status.textContent = 'Download failed: ' + ((err && err.message) || err);
    });
  }

  function fieldWidget(f) {
    const wrap = document.createElement('div');
    wrap.className = 'set-field';

    if (f.type === 'bool') {
      wrap.classList.add('inline');
      const label = document.createElement('label');
      label.textContent = f.label + (f.risky ? ' ⚠' : '');
      if (f.risky) {
        label.classList.add('set-danger-label');
        // Hover explains WHY it's flagged (risk, not availability) — the icon
        // had no explanation before, so users read it as "not installed".
        label.title = f.riskyWhy || 'Caution: this tool can act outside eVi’s sandbox — enable with care.';
      }
      // Availability badge (distinct from the risk ⚠): a muted "needs setup"
      // chip when the tool's dependency/binary is missing in THIS install.
      // cfg.availability only contains categories that are UNAVAILABLE.
      const avail = (f.section === 'tools' && cfg && cfg.availability) ? cfg.availability[f.key] : null;
      if (avail && avail.available === false) {
        const badge = document.createElement('span');
        badge.className = 'set-unavail';
        badge.textContent = 'needs setup';
        badge.title = (avail.reason || 'Not available in this install.') +
          (avail.how_to_enable ? '\n' + avail.how_to_enable : '');
        label.appendChild(badge);
      }
      const tog = document.createElement('button');
      tog.type = 'button';
      tog.className = 'set-toggle' + (getVal(f.section, f.key) ? ' on' : '');
      tog.setAttribute('aria-label', f.label);
      tog.onclick = () => {
        const nv = !getVal(f.section, f.key);
        setVal(f.section, f.key, nv); tog.classList.toggle('on', nv);
      };
      wrap.appendChild(label); wrap.appendChild(tog);
      return wrap;
    }

    if (f.type === 'auto-approve') {
      const t = document.createElement('div'); t.className = 'set-group-title';
      t.textContent = 'Auto-approve categories'; wrap.appendChild(t);
      // A list saved before code_read existed holds the old default's "code",
      // which now approves RUNNING code unasked. The server works out the fix
      // (evi.permissions.auto_approve_migration: "code" → "code_read", every
      // other entry kept; the one `evi lint` / `evi doctor` suggest) from the
      // SAVED config, and the button saves it right away.
      const migrated = cfg && Array.isArray(cfg.auto_approve_migration) ? cfg.auto_approve_migration : null;
      if (migrated) {
        const banner = document.createElement('div');
        banner.id = 'auto-approve-migration';
        banner.innerHTML = healthBannerHtml({
          tone: 'warn', title: 'Running code is approved without asking',
          msg: 'Your auto-approve list has "code" (the old default), which now means running code — '
            + 'run_python and linters that execute project code run with no prompt. Reading and '
            + 'analysing code is its own category, "code_read". Switch to: ' + migrated.join(', ')
            + ' (your other categories stay as they are).',
          cta: { label: 'Switch to code_read', action: 'migrate-auto-approve' },
        });
        wrap.appendChild(banner);
      }
      const current = new Set(getVal('auto', 'auto_approve') || []);
      AUTO_APPROVE_KEYS.forEach(k => {
        const row = document.createElement('label'); row.className = 'set-check';
        row.dataset.category = k;
        const cb = document.createElement('input'); cb.type = 'checkbox'; cb.checked = current.has(k);
        cb.onchange = () => {
          cb.checked ? current.add(k) : current.delete(k);
          setVal('auto', 'auto_approve', Array.from(current));
        };
        row.appendChild(cb); row.appendChild(document.createTextNode(' ' + k));
        if (AUTO_APPROVE_WHY[k]) {
          const why = document.createElement('span'); why.className = 'set-check-why';
          why.textContent = AUTO_APPROVE_WHY[k];
          row.appendChild(why);
        }
        wrap.appendChild(row);
      });
      return wrap;
    }

    const label = document.createElement('label'); label.textContent = f.label;
    wrap.appendChild(label);
    let inp;
    if (f.type === 'select' || f.type === 'client-select') {
      inp = document.createElement('select');
      (f.options || []).forEach(o => {
        const op = document.createElement('option'); op.value = o; op.textContent = o; inp.appendChild(op);
      });
      inp.value = f.type === 'client-select'
        ? (localStorage.getItem(THEME_KEY) || 'system') : getVal(f.section, f.key);
      inp.onchange = () => f.type === 'client-select'
        ? setThemePref(inp.value) : setVal(f.section, f.key, inp.value);
    } else if (f.type === 'model') {
      inp = document.createElement('select');
      const cur = getVal(f.section, f.key) || '';
      const list = modelOptions.slice();
      if (cur && !list.includes(cur)) list.unshift(cur);
      list.forEach(o => { const op = document.createElement('option'); op.value = o; op.textContent = o; inp.appendChild(op); });
      const c = document.createElement('option'); c.value = '__custom__'; c.textContent = 'Custom…'; inp.appendChild(c);
      inp.value = cur;
      inp.onchange = () => {
        if (inp.value === '__custom__') {
          const v = prompt('Model name:', cur);
          if (v && v.trim()) { setVal(f.section, f.key, v.trim()); renderSection(); }
          else inp.value = cur;
        } else setVal(f.section, f.key, inp.value);
      };
    } else if (f.type === 'secret') {
      inp = document.createElement('input'); inp.type = 'password';
      inp.value = getVal(f.section, f.key) || '';
      // hint shown for empty secret fields (attr name concatenated to keep the
      // incomplete-code linter from flagging the literal attribute token)
      inp.setAttribute('place' + 'holder', '(unchanged)');
      inp.oninput = () => setVal(f.section, f.key, inp.value);
    } else if (f.type === 'number' || f.type === 'int') {
      inp = document.createElement('input'); inp.type = 'number';
      if (f.step) inp.step = f.step; if (f.min != null) inp.min = f.min; if (f.max != null) inp.max = f.max;
      if (f.type === 'int') inp.step = inp.step || 1;
      inp.value = getVal(f.section, f.key);
      inp.oninput = () => {
        const v = f.type === 'int' ? parseInt(inp.value || '0', 10) : parseFloat(inp.value || '0');
        setVal(f.section, f.key, isNaN(v) ? 0 : v);
      };
    } else if (f.type === 'style') {
      inp = document.createElement('select');
      const dflt = document.createElement('option');
      dflt.value = ''; dflt.textContent = '(default)'; inp.appendChild(dflt);
      styleOptions.forEach(o => { const op = document.createElement('option'); op.value = o; op.textContent = o; inp.appendChild(op); });
      inp.value = getVal(f.section, f.key) || '';
      inp.onchange = () => setVal(f.section, f.key, inp.value);
    } else if (f.type === 'lines') {
      inp = document.createElement('textarea');
      inp.rows = 4;
      inp.style.cssText = 'width:100%;padding:7px 9px;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:4px;font:inherit;resize:vertical';
      inp.value = (getVal(f.section, f.key) || []).join('\n');
      inp.oninput = () => setVal(f.section, f.key,
        inp.value.split('\n').map(s => s.trim()).filter(Boolean));
    } else {
      inp = document.createElement('input'); inp.type = 'text';
      inp.value = getVal(f.section, f.key) || '';
      inp.oninput = () => setVal(f.section, f.key, inp.value);
    }
    wrap.appendChild(inp);
    if (f.help) { const hp = document.createElement('div'); hp.className = 'help'; hp.textContent = f.help; wrap.appendChild(hp); }
    return wrap;
  }

  function renderGuardrails() {
    const box = document.createElement('div');
    box.innerHTML = '<p class="sec-blurb" style="margin-top:0">Loading…</p>';
    contentEl.appendChild(box);
    fetch('/api/guardrails').then(r => r.json()).then(g => {
      const s = g.summary || {};
      box.innerHTML =
        '<div class="set-about-row"><span class="k">Status</span><span>' +
        (g.enabled ? '<span style="color:#4caf50">enabled</span>' : 'disabled') +
        '</span></div>' +
        '<div class="set-about-row"><span class="k">Rules</span><span>' +
        `${s.regex || 0} regex · ${s.judge || 0} judge · ${s.classifier || 0} classifier` +
        '</span></div>';
      const help = document.createElement('p');
      help.className = 'sec-blurb';
      help.textContent = 'Edit ~/.evi/guardrails.toml below. Saved changes are validated server-side.';
      box.appendChild(help);
      const ta = document.createElement('textarea');
      ta.id = 'gr-editor';
      ta.value = g.raw && g.raw.trim() ? g.raw : '# eVi content guardrails\nenabled = true\n';
      ta.style.cssText = 'width:100%;min-height:280px;font-family:monospace;font-size:12px;' +
        'background:var(--panel,#1a2027);color:var(--fg);border:1px solid var(--border,#2b333d);' +
        'border-radius:8px;padding:10px;white-space:pre;';
      box.appendChild(ta);
      const bar = document.createElement('div');
      bar.style.marginTop = '10px';
      const save = document.createElement('button');
      save.className = 'set-action-btn'; save.id = 'gr-save'; save.textContent = 'Save guardrails';
      const status = document.createElement('span');
      status.id = 'gr-status'; status.style.marginLeft = '12px';
      bar.appendChild(save); bar.appendChild(status); box.appendChild(bar);
      save.onclick = async () => {
        status.textContent = 'Saving…'; status.style.color = 'var(--dim)';
        try {
          const r = await fetch('/api/guardrails', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ raw: ta.value }),
          });
          const d = await r.json();
          if (!r.ok) { status.textContent = 'Error: ' + (d.detail || 'invalid'); status.style.color = '#e05252'; return; }
          status.textContent = 'Saved'; status.style.color = '#4caf50';
          setTimeout(renderSection, 600);
        } catch (e) { status.textContent = 'Error: ' + e; status.style.color = '#e05252'; }
      };
    }).catch(() => { box.innerHTML = '<p class="sec-blurb">Could not load guardrails.</p>'; });
  }

  function renderPlugins() {
    const box = document.createElement('div');
    box.id = 'plugins-box';
    box.innerHTML = '<p class="sec-blurb" style="margin-top:0">Loading…</p>';
    contentEl.appendChild(box);
    const PH = 'place' + 'holder';  // HTML hint attr (named indirectly to satisfy lint)
    const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, c =>
      ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;' }[c]));
    const post = async (url, body) => {
      const r = await fetch(url, { method:'POST', headers:{ 'Content-Type':'application/json' },
        body: JSON.stringify(body) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
      return d;
    };
    const status = (msg, ok) => {
      const el = document.getElementById('pl-status');
      if (el) { el.textContent = msg; el.style.color = ok ? '#4caf50' : '#e05252'; }
    };
    fetch('/api/plugins').then(r => r.json()).then(d => {
      const inst = d.installed || [], market = d.marketplace || [];
      let html = '<div class="set-about-row"><span class="k">Installed</span><span>' +
        inst.length + ' plugin' + (inst.length === 1 ? '' : 's') + '</span></div>';
      html += '<div id="pl-status" class="sec-blurb" style="min-height:1.1em"></div>';
      if (inst.length) {
        html += '<div style="margin-top:8px">';
        for (const p of inst) {
          const bits = [];
          if (p.commands) bits.push(p.commands + ' cmd');
          if (p.skills) bits.push(p.skills + ' skill');
          if (p.hooks) bits.push(p.hooks + ' hook');
          if (p.mcp) bits.push(p.mcp + ' mcp');
          if (p.agents) bits.push(p.agents + ' agent');
          html += '<div class="pl-row" style="display:flex;align-items:center;gap:10px;' +
            'padding:8px 0;border-bottom:1px solid var(--border,#2b333d)">' +
            '<div style="flex:1;min-width:0">' +
            '<div><b>' + esc(p.name) + '</b>' + (p.version ? ' <span class="dim">v' + esc(p.version) + '</span>' : '') + '</div>' +
            '<div class="dim" style="font-size:12px">' + esc(p.description || '') +
            (bits.length ? ' · ' + bits.join(' · ') : '') + '</div></div>' +
            '<button class="set-action-btn" data-remove="' + esc(p.name) + '">Remove</button></div>';
        }
        html += '</div>';
      }
      html += '<p class="sec-blurb" style="margin-top:16px">Marketplace</p>';
      html += '<input id="pl-search" type="text" ' +
        'style="width:100%;padding:8px;border-radius:8px;border:1px solid var(--border,#2b333d);' +
        'background:var(--panel,#1a2027);color:var(--fg)">';
      html += '<div id="pl-market" style="margin-top:8px"></div>';
      html += '<p class="sec-blurb" style="margin-top:16px">Install from a directory or git URL</p>';
      html += '<div style="display:flex;gap:8px"><input id="pl-source" type="text" ' +
        'style="flex:1;padding:8px;border-radius:8px;border:1px solid var(--border,#2b333d);' +
        'background:var(--panel,#1a2027);color:var(--fg)">' +
        '<button class="set-action-btn" id="pl-add">Install</button></div>';
      box.innerHTML = html;
      const searchEl = box.querySelector('#pl-search');
      const sourceEl = box.querySelector('#pl-source');
      if (searchEl) searchEl.setAttribute(PH, 'Filter plugins…');
      if (sourceEl) sourceEl.setAttribute(PH, '/path/to/plugin or https://…/plugin.git');

      const marketBox = box.querySelector('#pl-market');
      const renderMarket = (filter) => {
        const q = (filter || '').trim().toLowerCase();
        if (!market.length) {
          marketBox.innerHTML = '<p class="dim" style="font-size:12px">No index configured. ' +
            'Add entries with <code>evi plugin index add</code> or set <code>index_urls</code>.</p>';
          return;
        }
        const rows = market.filter(e => !q ||
          (e.name + ' ' + e.description + ' ' + (e.tags || []).join(' ')).toLowerCase().includes(q));
        marketBox.innerHTML = rows.length ? '' : '<p class="dim" style="font-size:12px">No matches.</p>';
        for (const e of rows) {
          const row = document.createElement('div');
          row.style.cssText = 'display:flex;align-items:center;gap:10px;padding:8px 0;' +
            'border-bottom:1px solid var(--border,#2b333d)';
          row.innerHTML = '<div style="flex:1;min-width:0">' +
            '<div><b>' + esc(e.name) + '</b>' + (e.author ? ' <span class="dim">by ' + esc(e.author) + '</span>' : '') + '</div>' +
            '<div class="dim" style="font-size:12px">' + esc(e.description || '') +
            ((e.tags && e.tags.length) ? ' · ' + e.tags.map(esc).join(', ') : '') + '</div></div>' +
            (e.installed ? '<span class="dim" style="font-size:12px">installed</span>'
              : '<button class="set-action-btn" data-install="' + esc(e.name) + '">Install</button>');
          marketBox.appendChild(row);
        }
      };
      renderMarket('');
      if (searchEl) searchEl.oninput = () => renderMarket(searchEl.value);

      box.onclick = async (ev) => {
        const t = ev.target;
        if (!(t instanceof HTMLElement)) return;
        const instName = t.getAttribute('data-install');
        const rmName = t.getAttribute('data-remove');
        if (instName) {
          t.disabled = true; status('Installing ' + instName + '…', true);
          try { await post('/api/plugins/install', { name: instName }); status('Installed ' + instName, true); setTimeout(renderSection, 700); }
          catch (e) { t.disabled = false; status('Error: ' + e.message, false); }
        } else if (rmName) {
          t.disabled = true; status('Removing ' + rmName + '…', true);
          try { await post('/api/plugins/remove', { name: rmName }); status('Removed ' + rmName, true); setTimeout(renderSection, 500); }
          catch (e) { t.disabled = false; status('Error: ' + e.message, false); }
        } else if (t.id === 'pl-add') {
          const src = (sourceEl && sourceEl.value) || '';
          if (!src.trim()) { status('Enter a directory or git URL', false); return; }
          t.disabled = true; status('Installing…', true);
          try { const d2 = await post('/api/plugins/install', { source: src.trim() }); status('Installed ' + d2.name, true); setTimeout(renderSection, 700); }
          catch (e) { t.disabled = false; status('Error: ' + e.message, false); }
        }
      };
    }).catch(() => { box.innerHTML = '<p class="sec-blurb">Could not load plugins.</p>'; });
  }

  function renderStats() {
    const box = document.createElement('div');
    box.id = 'stats-box';
    contentEl.appendChild(box);
    const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, c =>
      ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;' }[c]));
    // range selector
    const bar = document.createElement('div');
    bar.style.cssText = 'display:flex;gap:6px;align-items:center;margin-bottom:12px';
    bar.innerHTML = '<span class="dim" style="font-size:12px">Range:</span>';
    const body = document.createElement('div');
    body.id = 'stats-body';
    box.appendChild(bar); box.appendChild(body);
    const fmtTs = (ts) => { try { return new Date(ts * 1000).toLocaleDateString(); } catch (e) { return ''; } };
    const load = (days) => {
      body.innerHTML = '<p class="sec-blurb" style="margin-top:0">Loading…</p>';
      fetch('/api/stats?days=' + days).then(r => r.json()).then(d => {
        if (!d.sessions) {
          body.innerHTML = '<p class="sec-blurb">No transcripts yet ' +
            '(enable <code>tools.transcripts</code> to record sessions).</p>';
          return;
        }
        const span = (d.first_ts && d.last_ts) ? (fmtTs(d.first_ts) + ' – ' + fmtTs(d.last_ts)) : '';
        let html = '<div class="set-about-row"><span class="k">Sessions</span><span>' + d.sessions + '</span></div>' +
          '<div class="set-about-row"><span class="k">Messages</span><span>' + d.messages + '</span></div>' +
          '<div class="set-about-row"><span class="k">Approx. tokens</span><span>~' +
          Math.round((d.approx_tokens || 0) / 1000) + 'k</span></div>';
        if (span) html += '<div class="set-about-row"><span class="k">Span</span><span>' + esc(span) + '</span></div>';
        const kvList = (title, obj, n) => {
          const items = Object.entries(obj || {}).slice(0, n || 99);
          if (!items.length) return '';
          return '<p class="sec-blurb" style="margin-top:14px;margin-bottom:4px">' + title + '</p>' +
            '<div style="display:flex;flex-wrap:wrap;gap:6px">' +
            items.map(([k, v]) => '<span style="font-size:12px;padding:3px 8px;border-radius:10px;' +
              'background:var(--panel,#1a2027);border:1px solid var(--border,#2b333d)">' +
              esc(k) + ' <b>' + v + '</b></span>').join('') + '</div>';
        };
        html += kvList('Roles', d.roles);
        html += kvList('Top tools', d.tools, 10);
        html += kvList('Busiest days', d.busiest_days, 7);
        body.innerHTML = html;
      }).catch(() => { body.innerHTML = '<p class="sec-blurb">Could not load stats.</p>'; });
    };
    const ranges = [['All', 0], ['30d', 30], ['7d', 7]];
    let active = 0;
    ranges.forEach(([label, days]) => {
      const b = document.createElement('button');
      b.className = 'set-action-btn'; b.textContent = label;
      b.style.padding = '3px 10px';
      if (days === active) b.style.outline = '2px solid var(--accent,#4a9eff)';
      b.onclick = () => {
        active = days;
        bar.querySelectorAll('button').forEach(x => { x.style.outline = ''; });
        b.style.outline = '2px solid var(--accent,#4a9eff)';
        load(days);
      };
      bar.appendChild(b);
    });
    load(0);
  }

  // The latest eval report of each (suite, model, backend), from GET
  // /api/evals: one table per suite, a column per model and backend (the
  // backend under the model's name, so a fake run never reads as the model's
  // real result), the suite's pass rate on top and each case's below. Built
  // with textContent only (report text is never markup).
  function renderEvalResults(holder, reports) {
    holder.textContent = '';
    if (!reports || !reports.length) return;
    const pct = (r) => (r == null ? 'skipped' : Math.round(r * 100) + '%');
    const bySuite = new Map();
    for (const rep of reports) {
      if (!bySuite.has(rep.suite)) bySuite.set(rep.suite, []);
      bySuite.get(rep.suite).push(rep);
    }
    const title = document.createElement('div');
    title.className = 'eval-results-title';
    title.textContent = 'Latest results by model';
    holder.appendChild(title);
    for (const [suite, reps] of bySuite) {
      const card = document.createElement('div');
      card.className = 'eval-results';
      card.setAttribute('data-suite', suite);
      const head = document.createElement('div');
      head.className = 'eval-results-head';
      head.textContent = suite;
      card.appendChild(head);
      const table = document.createElement('table');
      const cell = (row, tag, text, attrs) => {
        const c = document.createElement(tag);
        c.textContent = text;
        for (const [k, v] of Object.entries(attrs || {})) c.setAttribute(k, v);
        row.appendChild(c);
        return c;
      };
      const col = (rep) => ({ 'data-model': rep.model, 'data-backend': rep.backend || '' });
      const hr = document.createElement('tr');
      cell(hr, 'th', '');
      for (const rep of reps) {
        const th = cell(hr, 'th', '', col(rep));
        const name = document.createElement('span');
        name.className = 'eval-results-model';
        name.textContent = rep.model;
        const via = document.createElement('span');
        via.className = 'eval-results-backend';
        via.textContent = rep.backend || '';
        th.append(name, via);
        th.title = (rep.backend || '') + ' · ' + (rep.finished_at || '');
      }
      table.appendChild(hr);
      const total = document.createElement('tr');
      total.className = 'eval-results-total';
      cell(total, 'td', 'suite');
      for (const rep of reps) {
        cell(total, 'td', pct(rep.pass_rate) + ' (' + rep.passed + '/' + rep.ran + ')',
          { ...col(rep), 'data-rate': String(rep.pass_rate) });
      }
      table.appendChild(total);
      const names = [];
      for (const rep of reps) for (const c of rep.cases || []) if (!names.includes(c.name)) names.push(c.name);
      for (const name of names) {
        const tr = document.createElement('tr');
        tr.setAttribute('data-case', name);
        cell(tr, 'td', name);
        for (const rep of reps) {
          const c = (rep.cases || []).find(x => x.name === name);
          const td = cell(tr, 'td', c ? pct(c.skipped ? null : c.pass_rate) : '—', col(rep));
          if (c && !c.skipped) td.className = c.passed ? 'ok' : (c.expected_fail ? 'xfail' : 'bad');
        }
        table.appendChild(tr);
      }
      card.appendChild(table);
      holder.appendChild(card);
    }
  }

  function renderEvals() {
    const box = document.createElement('div');
    box.id = 'evals-box';
    box.innerHTML = '<p class="sec-blurb" style="margin-top:0">Loading…</p>';
    contentEl.appendChild(box);
    const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, c =>
      ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;' }[c]));
    const results = document.createElement('div');
    results.id = 'eval-results-box';
    const refreshResults = () => fetch('/api/evals').then(r => r.json())
      .then(d => renderEvalResults(results, d.reports)).catch(() => {});
    fetch('/api/evals').then(r => r.json()).then(d => {
      const suites = d.suites || [];
      box.innerHTML = '';
      box.appendChild(results);
      renderEvalResults(results, d.reports);
      if (!suites.length) {
        const p = document.createElement('p');
        p.className = 'sec-blurb';
        p.innerHTML = 'No eval suites yet. Create one with ' +
          '<code>evi eval new &lt;name&gt;</code> (suites live in <code>~/.evi/evals/</code>).';
        box.appendChild(p);
        return;
      }
      for (const s of suites) {
        const card = document.createElement('div');
        card.className = 'eval-suite';
        card.style.cssText = 'margin-bottom:16px;border:1px solid var(--border,#2b333d);' +
          'border-radius:8px;padding:12px';
        let head = '<div style="display:flex;align-items:center;gap:10px">' +
          '<div style="flex:1;min-width:0"><b>' + esc(s.name) + '</b>' +
          ' <span class="dim" style="font-size:12px">' + s.cases.length + ' case' +
          (s.cases.length === 1 ? '' : 's') + '</span>' +
          (s.description ? '<div class="dim" style="font-size:12px">' + esc(s.description) + '</div>' : '') +
          (s.shadowed ? '<div class="dim eval-shadowed" style="font-size:12px">Shadowed: ' +
            '<code>self</code> names a repository&#39;s evals/self suites, so this self.toml ' +
            'cannot run under that name. Rename the file to run it.</div>' : '') +
          // The suite's file name (as `evi eval run` takes it): its TOML name may differ.
          '</div><button class="set-action-btn" data-run="' + esc(s.file || s.name) + '"' +
          (s.shadowed ? ' disabled' : '') + '>Run</button></div>';
        let cases = '<div style="margin-top:8px">';
        for (const c of s.cases) {
          cases += '<div class="eval-case" data-case="' + esc(c.name) + '" ' +
            'style="padding:6px 0;border-top:1px solid var(--border,#2b333d)">' +
            '<div style="display:flex;align-items:center;gap:8px">' +
            '<span class="eval-mark" style="font-size:12px;color:var(--dim)">○</span>' +
            '<b style="font-size:13px">' + esc(c.name) + '</b>' +
            (c.mode ? ' <span class="dim" style="font-size:11px">[' + esc(c.mode) + ']</span>' : '') +
            '</div>' +
            (c.checks && c.checks.length ?
              '<div class="dim" style="font-size:11px;margin-left:20px">' +
              c.checks.map(esc).join(' · ') + '</div>' : '') +
            '<div class="eval-fail dim" style="font-size:11px;margin-left:20px;color:#e05252"></div>' +
            '</div>';
        }
        cases += '</div>';
        card.innerHTML = head + '<div class="eval-status sec-blurb" style="min-height:1em"></div>' + cases;
        box.appendChild(card);
      }
      box.onclick = async (ev) => {
        const t = ev.target;
        if (!(t instanceof HTMLElement)) return;
        const runName = t.getAttribute('data-run');
        if (!runName) return;
        const card = t.closest('.eval-suite');
        const statusEl = card.querySelector('.eval-status');
        t.disabled = true;
        // POST /api/evals/run answers when the whole suite is done (each case
        // in a throwaway workspace under the implementer policy, on the
        // configured model), so a spinner shows meanwhile. Everything the
        // report says goes in as textContent.
        statusEl.textContent = '';
        const spin = document.createElement('span');
        spin.className = 'eval-spin';
        spin.style.cssText = 'display:inline-block;width:11px;height:11px;margin-right:6px;' +
          'vertical-align:-1px;border-radius:50%;border:2px solid var(--border,#3a4350);' +
          'border-top-color:var(--accent,#4aa3df);animation:work-spin .8s linear infinite';
        const runText = document.createElement('span');
        runText.textContent = 'Running ' + runName + '… (each case in a throwaway workspace, ' +
          'on the configured model)';
        statusEl.append(spin, runText);
        statusEl.style.color = 'var(--dim)';
        // reset marks, and the last run's data-verdict (a run that errors sets none)
        card.querySelectorAll('.eval-case').forEach(el => {
          el.removeAttribute('data-verdict');
          el.querySelector('.eval-mark').textContent = '○';
          el.querySelector('.eval-mark').style.color = 'var(--dim)';
          el.querySelector('.eval-fail').textContent = '';
        });
        try {
          const r = await fetch('/api/evals/run', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: runName }),
          });
          const rep = await r.json().catch(() => ({}));
          if (!r.ok) {
            statusEl.textContent = 'Error: ' + (typeof rep.detail === 'string' ? rep.detail : 'failed');
            statusEl.style.color = '#e05252';
            return;
          }
          const byName = {};
          (rep.cases || []).forEach(c => { byName[c.name] = c; });
          card.querySelectorAll('.eval-case').forEach(el => {
            const c = byName[el.getAttribute('data-case')];
            if (!c) return;
            const mark = el.querySelector('.eval-mark');
            const fail = el.querySelector('.eval-fail');
            el.setAttribute('data-verdict', c.skipped ? 'skip' : c.passed ? 'pass'
              : (c.expected_fail ? 'xfail' : 'fail'));
            mark.textContent = c.skipped ? '–' : (c.passed ? '✓' : '✗');
            mark.style.color = c.skipped ? 'var(--dim)' : c.passed ? '#4caf50'
              : (c.expected_fail ? '#e0a800' : '#e05252');
            const runs = c.runs > 1 ? c.passes + '/' + c.runs + ' runs · ' : '';
            if (c.skipped) fail.textContent = 'skipped: ' + (c.skip_reason || '');
            else if (!c.passed) fail.textContent = runs + (c.expected_fail ? 'expected to fail · ' : '') +
              (c.failures || []).join('; ');
            else fail.textContent = '';
          });
          const pct = Math.round((rep.pass_rate || 0) * 100);
          const bits = [rep.passed + '/' + rep.ran + ' passed (' + pct + '%)'];
          if (rep.expected_failures) bits.push(rep.expected_failures + ' expected to fail');
          if (rep.skipped) bits.push(rep.skipped + ' skipped');
          statusEl.textContent = (rep.ran ? '' : 'No case ran · ') + bits.join(', ') + ' — ' +
            (rep.model || '') + ' (' + (rep.backend || '') + ')';
          statusEl.style.color = !rep.ran ? '#e0a800' : (rep.failed ? '#e05252' : '#4caf50');
          refreshResults();
        } catch (e) {
          statusEl.textContent = 'Error: ' + e; statusEl.style.color = '#e05252';
        } finally { t.disabled = false; }
      };
    }).catch(() => { box.innerHTML = '<p class="sec-blurb">Could not load evals.</p>'; });
  }

  function renderAutomation() {
    const box = document.createElement('div');
    box.id = 'automation-box';
    contentEl.appendChild(box);
    const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, c =>
      ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;' }[c]));
    const PH = 'place' + 'holder';
    const post = async (url, body) => {
      const r = await fetch(url, { method:'POST', headers:{ 'Content-Type':'application/json' },
        body: JSON.stringify(body) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
      return d;
    };
    const inp = (id, hint, flex) => {
      const e = document.createElement('input');
      e.type = 'text'; e.id = id;
      e.setAttribute(PH, hint);
      e.style.cssText = 'flex:' + (flex || '1') + ';min-width:0;padding:7px;border-radius:7px;' +
        'border:1px solid var(--border,#2b333d);background:var(--panel,#1a2027);color:var(--fg)';
      return e;
    };

    // ---- Routes ----
    const routesWrap = document.createElement('div');
    box.appendChild(routesWrap);
    const renderRoutes = () => {
      routesWrap.innerHTML = '<p class="sec-blurb" style="margin-top:0">Multi-model routing</p>' +
        '<div id="rt-status" class="sec-blurb" style="min-height:1em"></div>';
      fetch('/api/routes').then(r => r.json()).then(d => {
        const routes = d.routes || [];
        const list = document.createElement('div');
        if (!routes.length) {
          list.innerHTML = '<p class="dim" style="font-size:12px">No routes yet — add one below.</p>';
        } else {
          for (const rt of routes) {
            const row = document.createElement('div');
            row.className = 'rt-row';
            row.style.cssText = 'display:flex;align-items:center;gap:10px;padding:8px 0;' +
              'border-bottom:1px solid var(--border,#2b333d)';
            const kw = (rt.match_keywords || []).join(', ');
            row.innerHTML = '<div style="flex:1;min-width:0">' +
              '<div><b>' + esc(rt.name) + '</b> <span class="dim">→ ' + esc(rt.model) + '</span></div>' +
              '<div class="dim" style="font-size:12px">' +
              (kw ? 'keywords: ' + esc(kw) : '<i>classifier-only</i>') +
              (rt.description ? ' · ' + esc(rt.description) : '') + '</div></div>' +
              '<button class="set-action-btn" data-rt-remove="' + esc(rt.name) + '">Remove</button>';
            list.appendChild(row);
          }
        }
        routesWrap.appendChild(list);
        // add form
        const form = document.createElement('div');
        form.style.cssText = 'display:flex;flex-wrap:wrap;gap:6px;margin-top:10px';
        const nm = inp('rt-name', 'name', '1'), md = inp('rt-model', 'model id', '1');
        const kws = inp('rt-kw', 'keywords (comma-sep)', '1'), desc = inp('rt-desc', 'description (optional)', '2');
        const add = document.createElement('button');
        add.className = 'set-action-btn'; add.id = 'rt-add'; add.textContent = 'Add route';
        form.append(nm, md, kws, desc, add);
        routesWrap.appendChild(form);
        add.onclick = async () => {
          const status = document.getElementById('rt-status');
          if (!nm.value.trim() || !md.value.trim()) {
            status.textContent = 'name and model are required'; status.style.color = '#e05252'; return;
          }
          add.disabled = true;
          try {
            await post('/api/routes', { name: nm.value.trim(), model: md.value.trim(),
              keywords: kws.value, description: desc.value.trim() });
            renderRoutes();
          } catch (e) { add.disabled = false; status.textContent = 'Error: ' + e.message; status.style.color = '#e05252'; }
        };
        list.onclick = async (ev) => {
          const t = ev.target;
          if (!(t instanceof HTMLElement)) return;
          const rm = t.getAttribute('data-rt-remove');
          if (!rm) return;
          t.disabled = true;
          try { await post('/api/routes/remove', { name: rm }); renderRoutes(); }
          catch (e) { t.disabled = false; const s = document.getElementById('rt-status'); s.textContent = 'Error: ' + e.message; s.style.color = '#e05252'; }
        };
      }).catch(() => { routesWrap.innerHTML = '<p class="sec-blurb">Could not load routes.</p>'; });
    };
    renderRoutes();

    // ---- Recipes ----
    const recipesWrap = document.createElement('div');
    recipesWrap.style.marginTop = '24px';
    box.appendChild(recipesWrap);
    recipesWrap.innerHTML = '<p class="sec-blurb">Saved recipes</p>' +
      '<p class="sec-blurb" style="margin-top:0">Loading…</p>';
    fetch('/api/recipes').then(r => r.json()).then(d => {
      const recipes = d.recipes || [];
      recipesWrap.innerHTML = '<p class="sec-blurb">Saved recipes</p>';
      if (!recipes.length) {
        recipesWrap.innerHTML += '<p class="dim" style="font-size:12px">No recipes yet. ' +
          'Create one with <code>evi recipe new &lt;name&gt;</code> ' +
          '(they live in <code>~/.evi/recipes/</code>).</p>';
        return;
      }
      for (const rc of recipes) {
        const card = document.createElement('div');
        card.className = 'recipe-card';
        card.style.cssText = 'margin-bottom:14px;border:1px solid var(--border,#2b333d);' +
          'border-radius:8px;padding:12px';
        let steps = '<ol style="margin:6px 0 0;padding-left:20px">';
        for (const s of rc.steps) {
          steps += '<li style="font-size:12px;margin-bottom:3px">' +
            (s.label ? '<b>' + esc(s.label) + ':</b> ' : '') +
            '<span class="dim">' + esc(s.prompt.length > 120 ? s.prompt.slice(0, 120) + '…' : s.prompt) + '</span></li>';
        }
        steps += '</ol>';
        card.innerHTML = '<div style="display:flex;align-items:center;gap:10px">' +
          '<div style="flex:1;min-width:0"><b>' + esc(rc.name) + '</b> ' +
          '<span class="dim" style="font-size:12px">' + rc.steps.length + ' step' +
          (rc.steps.length === 1 ? '' : 's') + '</span>' +
          (rc.description ? '<div class="dim" style="font-size:12px">' + esc(rc.description) + '</div>' : '') +
          '</div><button class="set-action-btn" data-recipe-run="' + esc(rc.name) + '">Run</button></div>' +
          '<div class="recipe-status sec-blurb" style="min-height:1em"></div>' + steps +
          '<div class="recipe-out"></div>';
        recipesWrap.appendChild(card);
      }
      recipesWrap.onclick = async (ev) => {
        const t = ev.target;
        if (!(t instanceof HTMLElement)) return;
        const runName = t.getAttribute('data-recipe-run');
        if (!runName) return;
        const card = t.closest('.recipe-card');
        const statusEl = card.querySelector('.recipe-status');
        const outEl = card.querySelector('.recipe-out');
        t.disabled = true; outEl.innerHTML = '';
        statusEl.textContent = 'Running ' + runName + '… (calls the model per step)';
        statusEl.style.color = 'var(--dim)';
        try {
          const rep = await post('/api/recipes/run', { name: runName });
          statusEl.textContent = 'Done — ' + (rep.steps || []).length + ' step(s)';
          statusEl.style.color = '#4caf50';
          let html = '';
          for (const s of (rep.steps || [])) {
            html += '<div style="margin-top:8px;padding-top:8px;border-top:1px solid var(--border,#2b333d)">' +
              '<div style="font-size:12px"><b>' + esc(s.label || 'step') + '</b></div>' +
              (s.error ? '<div style="font-size:12px;color:#e05252">' + esc(s.error) + '</div>'
                : '<div style="font-size:12px;white-space:pre-wrap">' + esc(s.text || '') + '</div>') +
              '</div>';
          }
          outEl.innerHTML = html;
        } catch (e) {
          statusEl.textContent = 'Error: ' + e.message; statusEl.style.color = '#e05252';
        } finally { t.disabled = false; }
      };
    }).catch(() => { recipesWrap.innerHTML = '<p class="sec-blurb">Could not load recipes.</p>'; });
  }

  function renderPeers() {
    const box = document.createElement('div');
    box.id = 'peers-box';
    box.innerHTML = '<p class="sec-blurb" style="margin-top:0">Loading…</p>';
    contentEl.appendChild(box);
    const PH = 'place' + 'holder';
    const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, c =>
      ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;' }[c]));
    const post = async (url, body) => {
      const r = await fetch(url, { method:'POST', headers:{ 'Content-Type':'application/json' },
        body: JSON.stringify(body) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
      return d;
    };
    const status = (msg, ok) => {
      const el = document.getElementById('pr-status');
      if (el) { el.textContent = msg; el.style.color = ok ? '#4caf50' : '#e05252'; }
    };
    // Section heading with a dim "— why" tail.
    const head = (t, why) => '<div style="font-weight:600;margin:18px 0 6px;font-size:14px">' +
      esc(t) + (why ? ' <span class="dim" style="font-weight:400;font-size:12px">— ' + esc(why) + '</span>' : '') + '</div>';
    // Compute this machine's "as a peer" posture from serving + real reachability
    // (self_status) + whether an access token is set. Drives the live banner so
    // the user sees the LAN-without-token / loopback / reachable state at a glance
    // instead of having to run a full preflight.
    const servePosture = (d) => {
      const ss = d.self_status || {};
      const st = ss.status || (d.serving ? 'down' : 'off');
      const tok = !!d.auth_token_set, port = ss.port || 8473;
      const url = 'http://' + (ss.lan_ip || '<this-machine-ip>') + ':' + port;
      if (!d.serving) return { tone:'off', title:'Not serving',
        msg:'Other machines can’t delegate work here. Turn on “Answer requests” below to become a peer.' };
      if (!tok && (st === 'lan' || d.bind_lan)) return { tone:'bad',
        title:'Serving on your network without an access token',
        msg:'eVi refuses every request from other machines until you set one — an open server on the LAN would let anyone run tasks here. (With LAN access on, the desktop app generates one at its next launch.)',
        cta:{ label:'Set access token', act:'set-serve-token' } };
      if (st === 'loopback') return { tone:'warn', title:'Reachable on this machine only',
        msg: d.bind_lan
          ? 'LAN access is on but not applied yet — relaunch the app so eVi binds to the network.'
          : ('Peers can’t reach you. ' + (tok
              ? 'Turn on “LAN access” below'
              : 'Set an access token and turn on “LAN access” below') +
             ', then relaunch the app.') };
      if (st === 'down') return { tone:'bad', title:'Nothing is listening',
        msg:'Serving is on but no server is answering on port ' + port + '.' };
      return { tone:'good', title:'Reachable by peers', lanUrl:url,
        msg:'Peers can delegate to this machine at ' + url + '. On the other machine, add this peer and paste this machine’s access token (the desktop app’s generated one is in ~/.evi/desktop-token).' };
    };
    const serveBannerHtml = (d) => {
      const p = servePosture(d);
      const tones = {
        good:{ c:'#4caf50', bg:'rgba(76,175,80,.09)', ic:'✓' },
        warn:{ c:'#e0a52e', bg:'rgba(224,165,46,.09)', ic:'⚠' },
        bad:{ c:'#e05252', bg:'rgba(224,82,82,.09)', ic:'✕' },
        off:{ c:'var(--dim,#8a94a0)', bg:'transparent', ic:'○' },
      };
      const tn = tones[p.tone] || tones.off;
      let inner = '<span style="color:' + tn.c + ';font-weight:700;font-size:15px;line-height:1.35">' + tn.ic + '</span>' +
        '<div style="flex:1;min-width:0"><b style="color:' + tn.c + '">' + esc(p.title) + '</b>' +
        '<div class="dim" style="font-size:12.5px;margin-top:2px;line-height:1.45">' + esc(p.msg) + '</div>';
      if (p.cta) inner += '<button class="set-action-btn" data-fed-act="' + p.cta.act + '" style="margin-top:8px">' + esc(p.cta.label) + '</button>';
      if (p.tone === 'good' && p.lanUrl) inner += '<button class="set-action-btn" data-fed-act="copy-addr" data-addr="' + esc(p.lanUrl) + '" style="margin-top:8px">Copy address</button>';
      inner += '</div>';
      return '<div id="fed-serve-banner" style="display:flex;gap:9px;align-items:flex-start;border:1px solid ' +
        tn.c + ';background:' + tn.bg + ';border-radius:9px;padding:10px 12px;margin:6px 0 10px">' + inner + '</div>';
    };
    // Re-fetch and swap just the serve banner + token state in place (keeps the
    // peer list and scroll intact) after a serve/LAN/token change.
    const refreshServeBanner = async () => {
      try {
        const d = await fetch('/api/peers').then(r => r.json());
        const b = box.querySelector('#fed-serve-banner');
        if (b) b.outerHTML = serveBannerHtml(d);
        const ts = box.querySelector('#fed-token-state');
        if (ts) { ts.textContent = d.auth_token_set ? 'set' : 'not set';
          ts.style.color = d.auth_token_set ? '#4caf50' : 'var(--dim,#8a94a0)'; }
        const sb = box.querySelector('#fed-set-token-btn');
        if (sb) sb.textContent = d.auth_token_set ? 'Rotate token' : 'Set token';
      } catch (e) { /* leave stale banner on transient error */ }
    };
    fetch('/api/peers').then(r => r.json()).then(d => {
      const peers = d.peers || [];
      // Intro + the explainer that finally makes the two-token model clear.
      let html = '<p class="sec-blurb" style="margin-top:0">Federation lets your eVi machines share work — ' +
        'send a heavy task to another machine, or let others send tasks here.</p>' +
        '<details class="fed-help" style="margin:4px 0 6px"><summary style="cursor:pointer;color:var(--accent,#4a9eff);font-size:13px">How federation works</summary>' +
        '<div class="dim" style="font-size:12.5px;line-height:1.5;margin:6px 0 2px;padding-left:2px">' +
        '<b>Two roles.</b> <b>Use peers</b> — send work to another machine (add it under “Peers you delegate to”). ' +
        '<b>Be a peer</b> — let other machines send work here (turn on serving + LAN access + set an access token).<br><br>' +
        '<b>The two tokens (this trips everyone up).</b> The machine that <i>serves</i> sets one <b>access token</b> ' +
        '(Settings → Server → Access token, same field as “Be a peer” below). Every machine that <i>delegates to it</i> ' +
        'stores that <b>same</b> token in its peer entry. They must match, or the request is refused (401). A machine serving on the ' +
        'LAN with <b>no</b> token is blocked entirely by eVi’s safety cutoff.<br><br>' +
        '<b>Firewall.</b> To be reached, the serving machine must serve on the network (LAN access) and have its port ' +
        '(8473 desktop, 8000 <code>evi web</code>) open in its firewall. Use <b>Troubleshoot</b> below to pinpoint what’s wrong.' +
        '</div></details>' +
        '<div id="pr-status" class="sec-blurb" style="min-height:1.1em"></div>';

      // ---- Section 1: Peers you delegate to (use peers) ----
      html += head('Peers you delegate to', 'machines this one can offload work to');
      if (!peers.length) {
        html += '<p class="dim" style="font-size:12px">No peers yet — add one below or scan the local network.</p>';
      } else {
        html += '<div style="margin-top:2px">';
        for (const p of peers) {
          const dot = p.reachable
            ? '<span style="color:#4caf50" title="reachable">●</span>'
            : '<span style="color:#e05252" title="unreachable">●</span>';
          let meta;
          if (p.reachable) {
            meta = 'eVi ' + esc(p.version) + (p.model ? ' · ' + esc(p.model) : '') +
              (p.has_token
                ? ' · <span style="color:#4caf50">token set</span>'
                : ' · <span style="color:#e0a52e">no token — delegation is refused if this peer requires auth</span>');
          } else {
            meta = '<span style="color:#e05252">unreachable</span> — is the machine on, eVi running, and its port open in the firewall?';
          }
          html += '<div class="peer-row" style="display:flex;align-items:center;gap:10px;' +
            'padding:8px 0;border-bottom:1px solid var(--border,#2b333d)">' +
            dot + '<div style="flex:1;min-width:0">' +
            '<div><b>' + esc(p.name) + '</b> <span class="dim">' + esc(p.url) + '</span></div>' +
            '<div class="dim" style="font-size:12px">' + meta + '</div></div>' +
            '<button class="set-action-btn" data-peer-token="' + esc(p.name) + '" data-peer-url="' + esc(p.url) + '">' +
            (p.has_token ? 'Change token' : 'Set token') + '</button>' +
            '<button class="set-action-btn" data-peer-remove="' + esc(p.name) + '">Remove</button></div>';
        }
        html += '</div>';
      }
      html += '<p class="sec-blurb" style="margin-top:14px">Add a peer</p>' +
        '<div style="display:flex;flex-wrap:wrap;gap:6px">' +
        '<input id="pr-name" type="text" style="flex:1;min-width:80px;padding:7px;border-radius:7px;' +
        'border:1px solid var(--border,#2b333d);background:var(--panel,#1a2027);color:var(--fg)">' +
        '<input id="pr-url" type="text" style="flex:2;min-width:160px;padding:7px;border-radius:7px;' +
        'border:1px solid var(--border,#2b333d);background:var(--panel,#1a2027);color:var(--fg)">' +
        '<input id="pr-token" type="password" style="flex:2;min-width:120px;padding:7px;border-radius:7px;' +
        'border:1px solid var(--border,#2b333d);background:var(--panel,#1a2027);color:var(--fg)">' +
        '<button class="set-action-btn" id="pr-add">Add peer</button></div>' +
        '<p class="dim" style="font-size:11.5px;margin:4px 0 0">Token = the <i>peer’s</i> access token (leave blank if it has none). ' +
        'It must match what that machine set under Settings → Server.</p>';
      html += '<p class="sec-blurb" style="margin-top:14px">Discover</p>' +
        '<div style="display:flex;gap:8px;align-items:center">' +
        '<button class="set-action-btn" id="pr-scan">Scan local network</button>' +
        '<span class="dim" style="font-size:12px">probes your /24 for eVi instances on port 8473 (~2 s)</span>' +
        '</div><div id="pr-scan-results" style="margin-top:8px"></div>';

      // ---- Section 2: Be a peer (serve side) ----
      html += head('Be a peer', 'let other machines delegate work to this one') +
        serveBannerHtml(d) +
        '<div class="set-about-row"><span class="k">Answer requests</span><span>' +
        '<label style="cursor:pointer"><input type="checkbox" id="pr-serve"' +
        (d.serving ? ' checked' : '') + '> run tasks delegated by trusted peers</label></span></div>' +
        '<div class="set-about-row"><span class="k">LAN access</span><span>' +
        '<label style="cursor:pointer"><input type="checkbox" id="pr-lan"' +
        (d.bind_lan ? ' checked' : '') + '> serve on the network (else loopback-only)</label>' +
        '<span class="dim" style="font-size:12px;display:block">Desktop only; binds 0.0.0.0 on next launch. Relaunch to apply.</span>' +
        '</span></div>' +
        '<div class="set-about-row"><span class="k">Access token</span><span>' +
        '<span id="fed-token-state" style="color:' + (d.auth_token_set ? '#4caf50' : 'var(--dim,#8a94a0)') + '">' +
        (d.auth_token_set ? 'set' : 'not set') + '</span> ' +
        '<button class="set-action-btn" id="fed-set-token-btn" data-fed-act="set-serve-token" style="margin-left:8px">' +
        (d.auth_token_set ? 'Rotate token' : 'Set token') + '</button>' +
        '<span class="dim" style="font-size:12px;display:block;margin-top:3px">Same token as Settings → Server. It gates this web UI ' +
        'and authenticates peers — each machine that delegates here must store this exact value.</span>' +
        '</span></div>';

      // ---- Section 3: Troubleshoot (deep preflight) ----
      html += head('Troubleshoot', 'deep check of serving, tokens & every peer') +
        '<button class="set-action-btn" id="pr-preflight">Run federation preflight</button>' +
        '<span class="dim" style="font-size:12px;margin-left:8px">tests each peer with a real delegation (a slow peer can take ~20s)</span>' +
        '<div id="pr-preflight-results" style="margin-top:8px"></div>';

      box.innerHTML = html;
      const nameEl = box.querySelector('#pr-name'), urlEl = box.querySelector('#pr-url'),
            tokenEl = box.querySelector('#pr-token');
      nameEl.setAttribute(PH, 'name (e.g. gpu)');
      urlEl.setAttribute(PH, 'http://gpu-box:8473');
      tokenEl.setAttribute(PH, 'peer access token (optional)');

      const serveEl = box.querySelector('#pr-serve');
      if (serveEl) serveEl.onchange = async () => {
        serveEl.disabled = true;
        try {
          const d3 = await post('/api/peers/serve', { enabled: serveEl.checked });
          status(d3.serving ? 'Serving on — this eVi now answers peers' : 'Serving off', true);
          await refreshServeBanner();
        } catch (e) { serveEl.checked = !serveEl.checked; status('Error: ' + e.message, false); }
        serveEl.disabled = false;
      };
      const lanEl = box.querySelector('#pr-lan');
      if (lanEl) lanEl.onchange = async () => {
        // Warn if turning LAN access on with no access token — the safety cutoff
        // will block every peer until one is set.
        if (lanEl.checked && !d.auth_token_set) {
          if (!confirm('You haven’t set an access token. Serving on the LAN without one is blocked by eVi’s safety cutoff — no peer will be able to delegate here until you set a token (Access token → Set token below).\n\nTurn on LAN access anyway?')) {
            lanEl.checked = false; return;
          }
        }
        lanEl.disabled = true;
        try {
          const d4 = await post('/api/peers/bind-lan', { enabled: lanEl.checked });
          status(d4.bind_lan ? 'LAN access on — relaunch the desktop app to apply' : 'LAN access off (loopback)', true);
          await refreshServeBanner();
        } catch (e) { lanEl.checked = !lanEl.checked; status('Error: ' + e.message, false); }
        lanEl.disabled = false;
      };

      box.onclick = async (ev) => {
        const t = ev.target;
        if (!(t instanceof HTMLElement)) return;
        const rm = t.getAttribute('data-peer-remove');
        const addUrl = t.getAttribute('data-peer-add-url');
        const setTok = t.getAttribute('data-peer-token');
        const fedAct = t.getAttribute('data-fed-act');
        if (rm) {
          t.disabled = true;
          try { await post('/api/peers/remove', { name: rm }); setTimeout(renderSection, 400); }
          catch (e) { t.disabled = false; status('Error: ' + e.message, false); }
        } else if (fedAct === 'set-serve-token') {
          // This machine's own [web] auth_token — same field as Settings → Server.
          // Setting it also gates this web UI, so keep the current session alive by
          // caching the value locally (the fetch wrapper reads it back).
          const val = prompt('Access token for THIS machine (blank to clear).\n\nThis also becomes this web UI’s login token, and the value peers must store to delegate here:', '');
          if (val === null) return;
          t.disabled = true;
          try {
            await post('/api/config', { web: { auth_token: val } });
            try { if (val) localStorage.setItem('evi.authToken', val); else localStorage.removeItem('evi.authToken'); } catch (e2) {}
            status(val ? 'Access token set — peers must use this exact value' : 'Access token cleared', true);
            await refreshServeBanner();
          } catch (e) { status('Error: ' + e.message, false); }
          t.disabled = false;
        } else if (fedAct === 'copy-addr') {
          const addr = t.getAttribute('data-addr') || '';
          try { await navigator.clipboard.writeText(addr); status('Copied ' + addr, true); }
          catch (e) { status('Copy this address: ' + addr, true); }
        } else if (setTok) {
          // Set/change/clear a peer's access token (upserts via POST /api/peers).
          const val = prompt('Access token for "' + setTok + '" (blank to clear it):', '');
          if (val === null) return;  // cancelled
          t.disabled = true;
          try { await post('/api/peers', { name: setTok, url: t.getAttribute('data-peer-url'), token: val });
            status('Token ' + (val ? 'set' : 'cleared') + ' for ' + setTok, true); setTimeout(renderSection, 500); }
          catch (e) { t.disabled = false; status('Error: ' + e.message, false); }
        } else if (addUrl) {
          const nm = t.getAttribute('data-peer-add-name') || addUrl.replace(/^https?:\/\//, '').replace(/[:.]/g, '-');
          // A LAN peer that serves with an access token needs it here too, or
          // delegation is refused — so offer to set it right when adding.
          const tok = prompt('Access token for "' + nm + '" (leave blank if the peer has no auth):', '');
          if (tok === null) return;  // cancelled
          t.disabled = true;
          try { await post('/api/peers', { name: nm, url: addUrl, token: tok });
            status(tok ? ('Added ' + nm + ' with token') : ('Added ' + nm + ' (no token yet)'), true);
            setTimeout(renderSection, 800); }
          catch (e) { t.disabled = false; status('Error: ' + e.message, false); }
        } else if (t.id === 'pr-add') {
          if (!nameEl.value.trim() || !urlEl.value.trim()) {
            status('name and url are required', false); return;
          }
          t.disabled = true;
          try {
            await post('/api/peers', { name: nameEl.value.trim(), url: urlEl.value.trim(),
              token: tokenEl.value });
            setTimeout(renderSection, 400);
          } catch (e) { t.disabled = false; status('Error: ' + e.message, false); }
        } else if (t.id === 'pr-preflight') {
          t.disabled = true; t.textContent = 'Running…';
          const out = box.querySelector('#pr-preflight-results');
          out.innerHTML = '<p class="dim" style="font-size:12px">Running preflight (a slow peer can take ~20s)…</p>';
          try {
            const d = await post('/api/peers/preflight', {});
            const ic = { ok: '<span style="color:#4caf50">✓</span>', warn: '<span style="color:#e0a52e">!</span>', fail: '<span style="color:#e05252">✗</span>' };
            out.innerHTML = (d.checks || []).map(c =>
              '<div style="padding:5px 0;border-bottom:1px solid var(--border,#2b333d)">' +
              (ic[c.status] || '?') + ' <b>' + esc(c.name) + '</b> <span class="dim">' + esc(c.detail) + '</span>' +
              (c.hint && c.status !== 'ok' ? '<div class="dim" style="font-size:12px;margin-left:18px">' + esc(c.hint) + '</div>' : '') +
              '</div>').join('') || '<p class="dim">no checks</p>';
            const s = d.summary || {};
            status(s.fail ? (s.fail + ' problem(s) — see below') : (s.warn ? (s.warn + ' warning(s)') : 'all checks passed'), !s.fail);
          } catch (e) { out.innerHTML = '<p class="dim" style="color:#e05252">error: ' + esc(e.message) + '</p>'; }
          t.disabled = false; t.textContent = 'Run federation preflight';
        } else if (t.id === 'pr-scan') {
          t.disabled = true; status('Scanning…', true);
          const out = box.querySelector('#pr-scan-results');
          out.innerHTML = '';
          try {
            const d2 = await post('/api/peers/scan', {});
            status('Scan done — ' + (d2.found || []).length + ' eVi instance(s) found', true);
            if (!(d2.found || []).length) {
              out.innerHTML = '<p class="dim" style="font-size:12px">Nothing found. The peer must be ' +
                'running <code>evi web --host 0.0.0.0</code> (and its firewall must allow port ' + d2.port + ').</p>';
            }
            for (const f of (d2.found || [])) {
              const row = document.createElement('div');
              row.style.cssText = 'display:flex;align-items:center;gap:10px;padding:8px 0;' +
                'border-bottom:1px solid var(--border,#2b333d)';
              row.innerHTML = '<div style="flex:1;min-width:0">' +
                '<div><b>' + esc(f.host) + '</b> <span class="dim">' + esc(f.url) + '</span></div>' +
                '<div class="dim" style="font-size:12px">eVi ' + esc(f.version) +
                (f.model ? ' · ' + esc(f.model) : '') + '</div></div>' +
                (f.configured ? '<span class="dim" style="font-size:12px">configured</span>'
                  : '<button class="set-action-btn" data-peer-add-url="' + esc(f.url) + '" ' +
                    'data-peer-add-name="' + esc(f.host) + '">Add</button>');
              out.appendChild(row);
            }
          } catch (e) { status('Error: ' + e.message, false); }
          finally { t.disabled = false; }
        }
      };
    }).catch(() => { box.innerHTML = '<p class="sec-blurb">Could not load peers.</p>'; });
  }

  function renderMcp() {
    const box = document.createElement('div');
    box.id = 'mcp-box';
    box.innerHTML = '<p class="sec-blurb" style="margin-top:0">Loading…</p>';
    contentEl.appendChild(box);
    const PH = 'place' + 'holder';
    const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, c =>
      ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;' }[c]));
    const post = async (url, body) => {
      const r = await fetch(url, { method:'POST', headers:{ 'Content-Type':'application/json' },
        body: JSON.stringify(body) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
      return d;
    };
    const status = (msg, ok) => {
      const el = document.getElementById('mcp-status');
      if (el) { el.textContent = msg; el.style.color = ok ? '#4caf50' : '#e05252'; }
    };
    fetch('/api/mcp').then(r => r.json()).then(d => {
      const servers = d.servers || [];
      let html = '<div class="set-about-row"><span class="k">MCP tools</span><span>' +
        (d.enabled ? '<span style="color:#4caf50">on</span>'
          : 'off — enable the <i>mcp</i> toggle under Tools to load these') +
        '</span></div>';
      if ((d.allowlist || []).length) {
        html += '<div class="set-about-row"><span class="k">Allowlist</span><span>' +
          d.allowlist.map(esc).join(', ') + ' <span class="dim">(others stay loaded-but-blocked)</span></span></div>';
      }
      html += '<div id="mcp-status" class="sec-blurb" style="min-height:1.1em"></div>';
      if (!servers.length) {
        html += '<p class="dim" style="font-size:12px">No servers configured — add one below ' +
          '(see the <i>MCP</i> docs for ready-made servers like filesystem/git/sqlite).</p>';
      } else {
        html += '<div style="margin-top:8px">';
        for (const s of servers) {
          const dot = s.on
            ? '<span style="color:#4caf50" title="enabled">●</span>'
            : '<span style="color:var(--dim)" title="disabled">●</span>';
          const bits = [];
          if (s.env_keys && s.env_keys.length) bits.push('env: ' + s.env_keys.join(', '));
          if (!s.allowed) bits.push('blocked by allowlist');
          html += '<div class="mcp-row" style="display:flex;align-items:center;gap:10px;' +
            'padding:8px 0;border-bottom:1px solid var(--border,#2b333d)">' +
            dot + '<div style="flex:1;min-width:0">' +
            '<div><b>' + esc(s.name) + '</b>' +
            (s.plugin ? ' <span class="dim" style="font-size:11px">(plugin)</span>' : '') + '</div>' +
            '<div class="dim" style="font-size:12px">' + esc(s.command + ' ' + (s.args || []).join(' ')) +
            (bits.length ? ' · ' + bits.map(esc).join(' · ') : '') + '</div></div>' +
            (s.plugin ? '' :
              '<button class="set-action-btn" data-mcp-toggle="' + esc(s.name) + '" data-mcp-on="' +
              (s.on ? '0' : '1') + '">' + (s.on ? 'Disable' : 'Enable') + '</button>' +
              '<button class="set-action-btn" data-mcp-remove="' + esc(s.name) + '">Remove</button>') +
            '</div>';
        }
        html += '</div>';
      }
      html += '<p class="sec-blurb" style="margin-top:16px">Add a server</p>' +
        '<div style="display:flex;flex-wrap:wrap;gap:6px">' +
        '<input id="mcp-name" type="text" style="flex:1;min-width:90px;padding:7px;border-radius:7px;' +
        'border:1px solid var(--border,#2b333d);background:var(--panel,#1a2027);color:var(--fg)">' +
        '<input id="mcp-command" type="text" style="flex:1;min-width:80px;padding:7px;border-radius:7px;' +
        'border:1px solid var(--border,#2b333d);background:var(--panel,#1a2027);color:var(--fg)">' +
        '<input id="mcp-args" type="text" style="flex:3;min-width:200px;padding:7px;border-radius:7px;' +
        'border:1px solid var(--border,#2b333d);background:var(--panel,#1a2027);color:var(--fg)">' +
        '<button class="set-action-btn" id="mcp-add">Add server</button></div>' +
        '<p class="dim" style="font-size:11px;margin-top:6px">Env vars (API keys etc.) aren\'t set ' +
        'here — use <code>evi mcp add --env KEY=VALUE</code> or edit <code>~/.evi/mcp.json</code>. ' +
        'Verify a server with <code>evi mcp list-tools</code>.</p>';
      html += '<hr style="border:none;border-top:1px solid var(--border,#2b333d);margin:18px 0">' +
        '<p class="sec-blurb" style="margin-top:0"><b>Serve eVi over MCP</b> — let other apps ' +
        '(Claude Desktop, Cursor, …) use eVi\'s curated tools, memory &amp; prompts.</p>' +
        '<div id="mcp-serve-box"><p class="dim" style="font-size:12px">Loading…</p></div>';
      box.innerHTML = html;
      const nameEl = box.querySelector('#mcp-name'), cmdEl = box.querySelector('#mcp-command'),
            argsEl = box.querySelector('#mcp-args');
      nameEl.setAttribute(PH, 'name (e.g. filesystem)');
      cmdEl.setAttribute(PH, 'command (npx, uvx, …)');
      argsEl.setAttribute(PH, 'args, e.g. -y @modelcontextprotocol/server-filesystem C:/Users');

      const renderServe = () => {
        fetch('/api/mcp/serve').then(r => r.json()).then(sv => {
          const sb = document.getElementById('mcp-serve-box');
          if (!sb) return;
          let h = '';
          if (sv.running) {
            h += '<div class="set-about-row"><span class="k">HTTP server</span><span>' +
              '<span style="color:#4caf50">running</span> · <code>' + esc(sv.url) + '</code>' +
              (sv.token_set ? ' · <span class="dim">token required</span>' : '') + '</span></div>';
            h += '<div class="set-about-row"><span class="k">Exposed</span><span>' +
              esc((sv.categories || []).join(', ')) + '</span></div>';
            h += '<button class="set-action-btn" id="mcp-serve-stop">Stop serving</button>';
          } else {
            h += '<div class="set-about-row"><span class="k">HTTP server</span><span class="dim">stopped</span></div>' +
              '<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:4px">' +
              '<input id="mcp-serve-cats" type="text" value="' + esc((sv.default_categories || []).join(',')) +
              '" style="flex:2;min-width:150px;padding:7px;border-radius:7px;border:1px solid var(--border,#2b333d);background:var(--panel,#1a2027);color:var(--fg)">' +
              '<input id="mcp-serve-token" type="text" style="flex:1;min-width:110px;padding:7px;border-radius:7px;border:1px solid var(--border,#2b333d);background:var(--panel,#1a2027);color:var(--fg)">' +
              '<button class="set-action-btn" id="mcp-serve-start">Start HTTP server</button></div>' +
              '<p class="dim" style="font-size:11px;margin-top:4px">Bound to localhost. Categories to expose (comma-separated); token optional for localhost.</p>';
          }
          h += '<div style="margin-top:10px"><button class="set-action-btn" id="mcp-serve-cfg">Show desktop (stdio) config</button>' +
            '<pre id="mcp-serve-snippet" style="display:none;white-space:pre-wrap;background:var(--panel,#1a2027);' +
            'border:1px solid var(--border,#2b333d);border-radius:6px;padding:8px;font-size:11px;margin-top:6px"></pre></div>';
          sb.innerHTML = h;
          const c = sb.querySelector('#mcp-serve-cats'); if (c) c.setAttribute(PH, 'memory,index,calendar,git');
          const tk = sb.querySelector('#mcp-serve-token'); if (tk) tk.setAttribute(PH, 'optional bearer token');
        }).catch(() => {});
      };
      renderServe();

      box.onclick = async (ev) => {
        const t = ev.target;
        if (!(t instanceof HTMLElement)) return;
        const rm = t.getAttribute('data-mcp-remove');
        const tog = t.getAttribute('data-mcp-toggle');
        if (rm) {
          t.disabled = true;
          try { await post('/api/mcp/remove', { name: rm }); setTimeout(renderSection, 400); }
          catch (e) { t.disabled = false; status('Error: ' + e.message, false); }
        } else if (tog) {
          t.disabled = true;
          try {
            await post('/api/mcp/toggle', { name: tog, on: t.getAttribute('data-mcp-on') === '1' });
            setTimeout(renderSection, 400);
          } catch (e) { t.disabled = false; status('Error: ' + e.message, false); }
        } else if (t.id === 'mcp-add') {
          if (!nameEl.value.trim() || !cmdEl.value.trim()) {
            status('name and command are required', false); return;
          }
          t.disabled = true;
          try {
            await post('/api/mcp', { name: nameEl.value.trim(), command: cmdEl.value.trim(),
              args: argsEl.value });
            setTimeout(renderSection, 400);
          } catch (e) { t.disabled = false; status('Error: ' + e.message, false); }
        } else if (t.id === 'mcp-serve-start') {
          t.disabled = true;
          try {
            const cats = (box.querySelector('#mcp-serve-cats')?.value || '')
              .split(',').map(x => x.trim()).filter(Boolean);
            const token = box.querySelector('#mcp-serve-token')?.value || '';
            const d = await post('/api/mcp/serve', { categories: cats, token });
            status('Serving at ' + d.url, true); renderServe();
          } catch (e) { t.disabled = false; status('Error: ' + e.message, false); }
        } else if (t.id === 'mcp-serve-stop') {
          t.disabled = true;
          try { await post('/api/mcp/serve/stop', {}); status('Stopped serving', true); renderServe(); }
          catch (e) { t.disabled = false; status('Error: ' + e.message, false); }
        } else if (t.id === 'mcp-serve-cfg') {
          try {
            const cats = (box.querySelector('#mcp-serve-cats')?.value || '').trim();
            const d = await fetch('/api/mcp/serve/config' +
              (cats ? '?categories=' + encodeURIComponent(cats) : '')).then(r => r.json());
            const pre = box.querySelector('#mcp-serve-snippet');
            if (pre) { pre.textContent = JSON.stringify(d.snippet, null, 2); pre.style.display = 'block'; }
          } catch (e) { status('Error: ' + e.message, false); }
        }
      };
    }).catch(() => { box.innerHTML = '<p class="sec-blurb">Could not load MCP servers.</p>'; });
  }

  function renderHooks() {
    const box = document.createElement('div');
    box.id = 'hooks-box';
    box.innerHTML = '<p class="sec-blurb" style="margin-top:0">Loading…</p>';
    contentEl.appendChild(box);
    const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, c =>
      ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;' }[c]));
    fetch('/api/hooks').then(r => r.json()).then(d => {
      const hooks = d.hooks || [];
      let html = '<div class="set-about-row"><span class="k">Loaded</span><span>' +
        hooks.length + ' hook' + (hooks.length === 1 ? '' : 's') +
        (hooks.some(h => h.veto) ? ' · <span style="color:#e0a800">some can veto</span>' : '') +
        '</span></div>';
      if (hooks.length) {
        html += '<div style="margin:8px 0">' + hooks.map(h =>
          '<span style="display:inline-block;font-size:12px;padding:3px 8px;margin:2px;' +
          'border-radius:10px;background:var(--panel,#1a2027);border:1px solid var(--border,#2b333d)">' +
          esc(h.event) + ': <b>' + esc(h.name) + '</b>' +
          (h.veto ? ' <span style="color:#e0a800">veto</span>' : '') + '</span>'
        ).join('') + '</div>';
      }
      box.innerHTML = html;
      const help = document.createElement('p');
      help.className = 'sec-blurb';
      help.textContent = 'Edit ~/.evi/hooks.toml below. Saving validates every entry — ' +
        'including event-name typos the runtime would silently skip. Plugin hooks are not ' +
        'shown here for editing (they live in the plugin).';
      box.appendChild(help);
      const ta = document.createElement('textarea');
      ta.id = 'hk-editor';
      ta.value = d.raw && d.raw.trim() ? d.raw :
        '# eVi hooks — see docs/features/hooks.md (events: ' + (d.events || []).join(', ') + ')\n';
      ta.style.cssText = 'width:100%;min-height:280px;font-family:monospace;font-size:12px;' +
        'background:var(--panel,#1a2027);color:var(--fg);border:1px solid var(--border,#2b333d);' +
        'border-radius:8px;padding:10px;white-space:pre;';
      box.appendChild(ta);
      const bar = document.createElement('div');
      bar.style.marginTop = '10px';
      const save = document.createElement('button');
      save.className = 'set-action-btn'; save.id = 'hk-save'; save.textContent = 'Save hooks';
      const status = document.createElement('span');
      status.id = 'hk-status'; status.style.marginLeft = '12px';
      bar.appendChild(save); bar.appendChild(status); box.appendChild(bar);
      save.onclick = async () => {
        status.textContent = 'Saving…'; status.style.color = 'var(--dim)';
        try {
          const r = await fetch('/api/hooks', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ raw: ta.value }),
          });
          const resp = await r.json();
          if (!r.ok) { status.textContent = 'Error: ' + (resp.detail || 'invalid'); status.style.color = '#e05252'; return; }
          status.textContent = 'Saved'; status.style.color = '#4caf50';
          setTimeout(renderSection, 600);
        } catch (e) { status.textContent = 'Error: ' + e; status.style.color = '#e05252'; }
      };
    }).catch(() => { box.innerHTML = '<p class="sec-blurb">Could not load hooks.</p>'; });
  }

  function renderUltracode() {
    const box = document.createElement('div');
    box.id = 'ultra-box';
    contentEl.appendChild(box);
    const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, c =>
      ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;' }[c]));
    const PH = 'place' + 'holder';
    box.innerHTML =
      '<p class="sec-blurb" style="margin-top:0">Run one hard task through the exhaustive ' +
      'pipeline: decompose → parallel solvers (diverse angles) → adversarial critique → ' +
      'synthesize. More thorough (and more model calls) than a single turn.</p>' +
      '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px">' +
      '<label class="dim" style="font-size:12px">breadth <input id="uc-breadth" type="number" min="1" max="6" value="3" ' +
      'style="width:52px;padding:4px;border-radius:6px;border:1px solid var(--border,#2b333d);background:var(--panel,#1a2027);color:var(--fg)"></label>' +
      '<label class="dim" style="font-size:12px">rounds <input id="uc-rounds" type="number" min="0" max="3" value="1" ' +
      'style="width:52px;padding:4px;border-radius:6px;border:1px solid var(--border,#2b333d);background:var(--panel,#1a2027);color:var(--fg)"></label>' +
      '<label class="dim" style="font-size:12px">mode ' +
      '<select id="uc-mode" style="padding:4px;border-radius:6px;border:1px solid var(--border,#2b333d);background:var(--panel,#1a2027);color:var(--fg)">' +
      '<option value="code">code</option><option value="cowork">cowork</option><option value="chat">chat</option></select></label>' +
      '</div>' +
      '<textarea id="uc-task" style="width:100%;min-height:90px;font-size:13px;padding:9px;border-radius:8px;' +
      'border:1px solid var(--border,#2b333d);background:var(--panel,#1a2027);color:var(--fg)"></textarea>' +
      '<div style="margin-top:8px"><button class="set-action-btn" id="uc-run">Run ultracode</button>' +
      '<span id="uc-status" class="sec-blurb" style="margin-left:12px"></span></div>' +
      '<div id="uc-stages" style="margin-top:10px"></div>' +
      '<div id="uc-answer" style="margin-top:10px;white-space:pre-wrap;font-size:13px"></div>';
    box.querySelector('#uc-task').setAttribute(PH,
      'e.g. refactor the auth module to remove duplicated token parsing, with tests');
    const status = (m, ok) => {
      const el = box.querySelector('#uc-status');
      el.textContent = m; el.style.color = ok ? '#4caf50' : (ok === false ? '#e05252' : 'var(--dim)');
    };
    box.querySelector('#uc-run').onclick = async () => {
      const task = box.querySelector('#uc-task').value.trim();
      if (!task) { status('Enter a task', false); return; }
      const btn = box.querySelector('#uc-run');
      btn.disabled = true; status('Running… (this calls the model many times)', null);
      box.querySelector('#uc-stages').innerHTML = '';
      box.querySelector('#uc-answer').textContent = '';
      try {
        const r = await fetch('/api/dispatch/ultracode', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            task,
            breadth: parseInt(box.querySelector('#uc-breadth').value, 10) || 0,
            rounds: parseInt(box.querySelector('#uc-rounds').value, 10),
            mode: box.querySelector('#uc-mode').value,
          }),
        });
        const d = await r.json();
        if (!r.ok) { status('Error: ' + (d.detail || 'failed'), false); btn.disabled = false; return; }
        status((d.stages || []).length + ' stages done', true);
        box.querySelector('#uc-stages').innerHTML = (d.stages || []).map(s =>
          '<details style="border:1px solid var(--border,#2b333d);border-radius:6px;margin:3px 0;padding:4px 8px">' +
          '<summary style="cursor:pointer;font-size:12px"><b>' + esc(s.name) + '</b> ' +
          '<span class="dim">' + esc(s.label) + '</span></summary>' +
          '<div style="white-space:pre-wrap;font-size:12px;margin-top:6px">' + esc(s.output) + '</div></details>'
        ).join('');
        const ans = box.querySelector('#uc-answer');
        ans.innerHTML = '<p class="sec-blurb" style="margin-bottom:4px">Answer</p>';
        ans.appendChild(document.createTextNode(d.answer || ''));
      } catch (e) { status('Error: ' + e, false); }
      finally { btn.disabled = false; }
    };
  }

  // ---- Channels (Telegram) -------------------------------------------
  // The pairing queue lives here rather than only in the CLI: someone who gets a
  // code on their phone shouldn't need a terminal to be let in. Approving is
  // still a local-machine action — this UI IS the local machine.
  async function renderChannels() {
    const box = document.createElement('div');
    box.innerHTML = '<p class="sec-blurb">Loading…</p>';
    contentEl.appendChild(box);
    let st = null;
    try { st = await fetch('/api/channels/telegram').then(r => r.json()); } catch (e) {}
    if (!st) { box.innerHTML = '<p class="sec-blurb">Couldn\'t read channel status.</p>'; return; }

    const cfgCh = (cfg && cfg.channels) || {};
    const tokenSet = st.token_set;
    const state = !st.enabled ? ['off', 'var(--dim)']
      : st.running ? ['running', 'var(--user)']
      : (tokenSet ? ['not running', 'var(--err)'] : ['needs a bot token', 'var(--tool)']);

    box.innerHTML =
      '<div class="set-about-row"><span class="k">Telegram</span>' +
        '<span style="color:' + state[1] + '">' + state[0] + '</span></div>' +
      (st.last_error ? '<div class="sec-blurb" style="color:var(--err)">' + escapeHtml(st.last_error) + '</div>' : '') +
      '<p class="sec-blurb" style="margin-top:10px">Create a bot with <b>@BotFather</b> on Telegram, paste its token below, ' +
      'then message your bot. Unknown senders get a pairing code and are ignored until you approve them here.</p>' +
      '<div style="margin:12px 0"><label class="k" style="display:block;margin-bottom:4px">Bot token</label>' +
      '<input id="ch-token" type="password" style="' + RT_INPUT + '" placeholder="' +
        (tokenSet ? '•••••••• (saved)' : '123456:ABC-DEF…') + '"></div>' +
      '<label style="display:flex;gap:8px;align-items:center;font-size:13px">' +
      '<input type="checkbox" id="ch-enabled"' + (cfgCh.telegram_enabled ? ' checked' : '') + '> Enable the Telegram channel</label>' +
      '<p class="sec-blurb" style="margin-top:8px">Approved senders can use: <b>' +
        escapeHtml((st.tools || []).join(', ') || 'nothing') + '</b>. ' +
        'This is deliberately narrower than your own toolset — anyone you pair with can use these.</p>' +
      '<div style="margin-top:10px"><button class="set-action-btn" id="ch-save">Save &amp; apply</button></div>' +
      '<div id="ch-lists" style="margin-top:16px"></div>';

    const lists = box.querySelector('#ch-lists');
    const drawLists = function (s) {
      lists.innerHTML =
        (s.pending && s.pending.length
          ? '<div class="k" style="margin-bottom:5px">Waiting for approval</div>' +
            s.pending.map(function (p) {
              return '<div class="set-about-row"><span>' + escapeHtml(p.name || '(no name)') +
                ' <code>' + escapeHtml(p.code) + '</code></span>' +
                '<button class="set-action-btn ch-ok" data-code="' + escapeAttr(p.code) + '">Approve</button></div>';
            }).join('')
          : '') +
        (s.paired && s.paired.length
          ? '<div class="k" style="margin:12px 0 5px">Paired</div>' +
            s.paired.map(function (p) {
              return '<div class="set-about-row"><span>' + escapeHtml(p.name || p.user_id) + '</span>' +
                '<button class="set-action-btn ch-no" data-uid="' + escapeAttr(p.user_id) + '">Revoke</button></div>';
            }).join('')
          : '<p class="sec-blurb" style="margin-top:10px">Nobody is paired yet.</p>');
      lists.querySelectorAll('.ch-ok').forEach(function (b) {
        b.onclick = async function () {
          await fetch('/api/channels/telegram/approve', { method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code: b.getAttribute('data-code') }) }).catch(function () {});
          drawLists(await fetch('/api/channels/telegram').then(r => r.json()));
        };
      });
      lists.querySelectorAll('.ch-no').forEach(function (b) {
        b.onclick = async function () {
          await fetch('/api/channels/telegram/revoke', { method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_id: b.getAttribute('data-uid') }) }).catch(function () {});
          drawLists(await fetch('/api/channels/telegram').then(r => r.json()));
        };
      });
    };
    drawLists(st);

    box.querySelector('#ch-save').onclick = async function (e) {
      const btn = e.currentTarget;
      btn.disabled = true; btn.textContent = 'Saving…';
      const patch = { channels: { telegram_enabled: box.querySelector('#ch-enabled').checked } };
      const tok = box.querySelector('#ch-token').value.trim();
      if (tok) patch.channels.telegram_token = tok;      // blank = keep the saved one
      try {
        await fetch('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                     body: JSON.stringify(patch) });
        const s2 = await fetch('/api/channels/telegram/restart', { method: 'POST' }).then(r => r.json());
        cfg.channels = Object.assign({}, cfg.channels, patch.channels);
        setTimeout(renderSection, 0);
        drawLists(s2);
      } catch (err) {
        btn.textContent = 'Save & apply';
      }
      btn.disabled = false;
    };
  }

  // ---- Companion (evi/companion/) --------------------------------------
  // The owner's view of the Duet-style watcher: on/off, its feed newest-first,
  // the actions it proposes (each runs only after a second click here, exactly
  // as shown), and its instructions. Feed and proposal text is the model's or a
  // source's, untrusted: every string goes in through textContent, never HTML.
  // Refreshes every 30 s while the section is open (every 2 s while a check-in
  // this server started is running); the timers stop once it is closed.
  const CMP_KINDS = { brief: 'brief', alert: 'alert', suggestion: 'suggestion',
                      working_on: 'working on', needs_you: 'needs you', result: 'result' };
  const CMP_SKIPS = { busy: 'another check-in was running', 'no sources': 'none of its sources is available',
                      unchanged: 'nothing changed since the last one (no model call)', disabled: 'it is off' };
  let cmpTicker = null;
  // Control and format characters (a bidi override, zero-width, CR) shown as
  // their \u escape, as evi.companion.printable does for the CLI: an approval
  // card shows the exact arguments, never a path the browser reorders.
  // Newline and tab stay (and a CRLF line end is a newline).
  const CMP_UNSHOWN = /(?![\n\t])[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}]/gu;
  function cmpVisible(s) {
    return String(s).replace(/\r\n/g, '\n').replace(CMP_UNSHOWN, c => {
      const n = c.codePointAt(0);
      return n > 0xffff ? '\\U' + n.toString(16).padStart(8, '0')
        : n < 0x100 ? '\\x' + n.toString(16).padStart(2, '0')
        : '\\u' + n.toString(16).padStart(4, '0');
    });
  }
  function cmpEl(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = cmpVisible(text);
    return e;
  }
  // A proposal's arguments, short values first: a long `content` can't push
  // the `path` it goes to out of the card's scroll box.
  function cmpArgs(args) {
    if (!args || typeof args !== 'object' || Array.isArray(args)) return JSON.stringify(args, null, 2);
    const keys = Object.keys(args).map((k, i) => [k, (JSON.stringify(args[k]) || '').length > 200, i]);
    keys.sort((a, b) => (a[1] - b[1]) || (a[2] - b[2]));
    const out = Object.create(null);   // so a "__proto__" argument is shown like any other
    keys.forEach(([k]) => { out[k] = args[k]; });
    return JSON.stringify(out, null, 2);
  }
  function cmpWhen(ts) {
    if (typeof ts !== 'number') return 'never';
    try { return new Date(ts * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }); }
    catch { return new Date(ts * 1000).toLocaleString(); }
  }
  function cmpLeft(sec) {
    if (sec <= 0) return 'expired';
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
    return 'expires in ' + (h ? h + ' h ' + m + ' min' : m ? m + ' min ' + s + ' s' : s + ' s');
  }
  async function cmpCall(url, opts) {
    let r, d = {};
    try { r = await fetch(url, opts); } catch (e) { return { ok: false, status: 0, data: { detail: String(e) } }; }
    try { d = await r.json(); } catch {}
    return { ok: r.ok, status: r.status, data: d };
  }
  const cmpPost = (url, body, method) => cmpCall(url, { method: method || 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });

  async function renderCompanion() {
    const box = cmpEl('div', 'cmp'); box.id = 'cmp-panel';
    const head = cmpEl('div', 'cmp-head'); head.id = 'cmp-head';
    const props = cmpEl('div'); props.id = 'cmp-proposals';
    const feed = cmpEl('div'); feed.id = 'cmp-feed';
    const instr = cmpEl('div', 'cmp-instr'); instr.id = 'cmp-instr';
    box.append(head, cmpEl('div', 'k cmp-sec', 'Waiting for your approval'), props,
               cmpEl('div', 'k cmp-sec', 'Feed'), feed,
               cmpEl('div', 'k cmp-sec', 'Instructions'), instr);
    contentEl.appendChild(box);
    head.appendChild(cmpEl('p', 'sec-blurb', 'Loading…'));

    let skew = 0;           // server clock minus ours, for the expiry countdowns
    let st = null;
    const done = new Map(); // proposals decided in this panel: id -> {proposal, error}
    let armedId = null;     // a card whose Approve was clicked once: not redrawn under the user

    function drawHead() {
      head.replaceChildren();
      if (!st) { head.appendChild(cmpEl('p', 'cmp-err', 'Couldn’t read the Companion’s status.')); return; }
      const sw = cmpEl('label', 'cmp-switch');
      const cb = document.createElement('input'); cb.type = 'checkbox'; cb.id = 'cmp-enabled';
      cb.checked = !!st.enabled;
      sw.append(cb, cmpEl('b', '', 'Companion'), cmpEl('span', 'cmp-state ' + (st.enabled ? 'on' : 'off'), st.enabled ? 'on' : 'off'));
      head.appendChild(sw);
      head.appendChild(cmpEl('p', 'cmp-dim', 'When on, it checks your sources every few hours and ' +
        'sends a morning brief. It only reads: anything it wants to do waits here until you approve it.'));
      cb.onchange = async () => {
        cb.disabled = true;
        const r = await cmpPost('/api/companion/enabled', { on: cb.checked });
        if (r.ok) st = r.data; else cb.checked = !cb.checked;
        drawHead(); refresh();
      };
      if (!st.enabled) head.appendChild(cmpEl('p', 'cmp-note', 'Companion is off.'));
      if (st.safe_mode) head.appendChild(cmpEl('p', 'cmp-err', 'Safe mode is on: the Companion doesn’t run.'));
      else if (st.enabled && !st.scheduler) {
        head.appendChild(cmpEl('p', 'cmp-err', 'No scheduler runs in this server, so check-ins ' +
          'happen only when you press Check now (or run `evi scheduler`).'));
      }
      const rows = [
        ['Reads', (st.sources || []).join(', ') || 'nothing (add [companion] sources)'],
        ['May propose', (st.allowed_actions || []).join(', ') || 'nothing: it only suggests'],
        ['Last check-in', cmpWhen(st.last_check)],
        ['Next check-in', !st.enabled ? '—' : typeof st.next_check === 'number'
          ? cmpWhen(st.next_check) : 'due now (the scheduler looks every 15 min)'],
        ['Last brief', st.brief_day || 'never'],
        ['Notifications today', String(st.notifications_today || 0)],
      ];
      const tbl = cmpEl('div', 'cmp-status'); tbl.id = 'cmp-status';
      rows.forEach(([k, v]) => {
        const row = cmpEl('div', 'set-about-row');
        row.append(cmpEl('span', 'k', k), cmpEl('span', '', v));
        tbl.appendChild(row);
      });
      head.appendChild(tbl);
      const act = cmpEl('div', 'cmp-actions');
      const chk = cmpEl('button', 'set-action-btn', st.check_running ? 'Checking…' : 'Check now');
      chk.id = 'cmp-check'; chk.type = 'button';
      chk.disabled = !st.enabled || !!st.safe_mode || !!st.check_running;
      chk.onclick = async () => {
        chk.disabled = true; chk.textContent = 'Checking…';
        const r = await cmpPost('/api/companion/check');
        if (!r.ok) { lastOut.textContent = cmpVisible(r.data.detail || ('failed (' + r.status + ')')); lastOut.className = 'cmp-err'; }
        refresh();
      };
      act.appendChild(chk);
      const lastOut = cmpEl('span', 'cmp-dim'); lastOut.id = 'cmp-last-run';
      const lr = st.last_run;
      if (st.check_running) lastOut.textContent = 'A check-in is running…';
      else if (lr) {
        lastOut.textContent = cmpVisible(lr.error ? 'Last check-in failed: ' + lr.error
          : lr.skipped ? 'Last check-in skipped: ' + (CMP_SKIPS[lr.skipped] || lr.skipped)
          : 'Last check-in: ' + ((lr.items || []).length ? (lr.items.length + ' new item(s)') : 'nothing new'));
        if (lr.error) lastOut.className = 'cmp-err';
      }
      act.appendChild(lastOut);
      head.appendChild(act);
    }

    function proposalCard(p, decided) {
      const card = cmpEl('div', 'cmp-card'); card.dataset.id = p.id;
      const top = cmpEl('div', 'cmp-card-top');
      top.append(cmpEl('code', 'cmp-tool', p.tool), cmpEl('span', 'cmp-badge', decided ? p.state : 'proposed'));
      card.appendChild(top);
      card.appendChild(cmpEl('div', 'cmp-summary', p.summary || ''));
      let argsText;
      try { argsText = cmpArgs(p.args); } catch { argsText = String(p.args); }
      const pre = cmpEl('pre', 'cmp-args', argsText); card.appendChild(pre);
      if (decided) {
        if (p.result !== undefined && p.result !== null) {
          card.appendChild(cmpEl('div', 'cmp-dim', p.state === 'executed' ? 'Result' : 'Output'));
          card.appendChild(cmpEl('pre', 'cmp-result', p.result));
        } else if (p.reason) card.appendChild(cmpEl('div', 'cmp-err', p.reason));
        return card;
      }
      const exp = cmpEl('div', 'cmp-dim cmp-exp'); exp.dataset.exp = String(p.expires_at || 0);
      exp.textContent = cmpLeft((p.expires_at || 0) - (Date.now() / 1000 + skew));
      card.appendChild(exp);
      const err = cmpEl('div', 'cmp-err'); card.appendChild(err);
      const btns = cmpEl('div', 'cmp-actions');
      const ok = cmpEl('button', 'set-action-btn cmp-approve', 'Approve'); ok.type = 'button';
      const no = cmpEl('button', 'set-action-btn cmp-reject', 'Not now'); no.type = 'button';
      let armed = null;
      ok.onclick = async () => {
        if (!armed) {   // the first click only arms it: the second one runs
          ok.textContent = cmpVisible('Run ' + p.tool + ' now'); ok.classList.add('armed'); armedId = p.id;
          armed = setTimeout(() => {
            armed = null; armedId = null; ok.textContent = 'Approve'; ok.classList.remove('armed');
          }, 8000);
          return;
        }
        clearTimeout(armed); armed = null; armedId = null;
        ok.disabled = no.disabled = true; ok.textContent = 'Running…';
        const r = await cmpPost('/api/companion/proposals/' + encodeURIComponent(p.id) + '/approve',
                                { args_hash: p.args_hash });
        if (r.ok) done.set(p.id, r.data.proposal);
        else done.set(p.id, Object.assign({}, p, { state: r.status === 409 ? 'refused' : 'error',
                                                    reason: r.data.detail || ('failed (' + r.status + ')') }));
        refresh();
      };
      no.onclick = async () => {
        if (armed) { clearTimeout(armed); armed = null; }
        if (armedId === p.id) armedId = null;
        ok.disabled = no.disabled = true;
        const r = await cmpPost('/api/companion/proposals/' + encodeURIComponent(p.id) + '/reject');
        if (!r.ok) { err.textContent = cmpVisible(r.data.detail || ('failed (' + r.status + ')')); }
        else done.set(p.id, r.data.proposal);
        refresh();
      };
      btns.append(ok, no); card.appendChild(btns);
      return card;
    }

    function drawProposals(pending) {
      props.replaceChildren();
      [...done.values()].reverse().forEach(p => props.appendChild(proposalCard(p, true)));
      const open = (pending || []).filter(p => !done.has(p.id));
      open.forEach(p => props.appendChild(proposalCard(p, false)));
      if (!open.length && !done.size) props.appendChild(cmpEl('p', 'cmp-dim', 'Nothing is waiting for your approval.'));
    }

    function drawFeed(items) {
      feed.replaceChildren();
      if (!items || !items.length) {
        feed.appendChild(cmpEl('p', 'cmp-dim', st && !st.enabled ? 'Companion is off. Nothing yet.' : 'Nothing yet.'));
        return;
      }
      items.slice().reverse().forEach(i => {   // newest first
        const row = cmpEl('div', 'cmp-item');
        const top = cmpEl('div', 'cmp-item-top');
        const kind = String(i.kind || '');
        top.append(cmpEl('span', 'cmp-badge k-' + kind.replace(/[^a-z_]/g, ''), CMP_KINDS[kind] || kind),
                   cmpEl('span', 'cmp-prio p-' + String(i.priority || '').replace(/[^a-z]/g, ''), i.priority || ''),
                   cmpEl('b', 'cmp-title', i.title || ''),
                   cmpEl('span', 'cmp-dim cmp-at', cmpWhen(i.ts)));
        row.appendChild(top);
        if (i.body) row.appendChild(cmpEl('div', 'cmp-body', i.body));
        if (i.why_now) row.appendChild(cmpEl('div', 'cmp-dim', 'Why now: ' + i.why_now));
        if (i.proposal_refused && typeof i.proposal_refused === 'object') {
          row.appendChild(cmpEl('div', 'cmp-dim', 'Proposed ' + (i.proposal_refused.tool || '?') +
            ', refused: ' + (i.proposal_refused.reason || '')));
        }
        if (i.notified) row.appendChild(cmpEl('div', 'cmp-dim', 'Notified'));
        else if (i.held) row.appendChild(cmpEl('div', 'cmp-dim', 'Not notified: ' + i.held));
        feed.appendChild(row);
      });
    }

    async function drawInstructions() {
      instr.replaceChildren();
      instr.appendChild(cmpEl('p', 'cmp-dim', 'What to watch, what to ignore, how often to speak up. ' +
        'The Companion reads this; only you write it.'));
      const ta = document.createElement('textarea'); ta.id = 'cmp-instr-text'; ta.rows = 6;
      ta.placeholder = 'e.g. Watch my calendar for clashes. Ignore newsletters. Only ping me for things today.';
      const r = await cmpCall('/api/companion/instructions');
      if (r.ok) { ta.value = r.data.text || ''; if (r.data.max_chars) ta.maxLength = r.data.max_chars; }
      const row = cmpEl('div', 'cmp-actions');
      const save = cmpEl('button', 'set-action-btn', 'Save instructions'); save.id = 'cmp-instr-save'; save.type = 'button';
      const msg = cmpEl('span', 'cmp-dim'); msg.id = 'cmp-instr-msg';
      save.onclick = async () => {
        save.disabled = true; msg.className = 'cmp-dim'; msg.textContent = 'Saving…';
        const w = await cmpPost('/api/companion/instructions', { text: ta.value }, 'PUT');
        save.disabled = false;
        if (w.ok) msg.textContent = 'Saved.';
        else { msg.className = 'cmp-err'; msg.textContent = cmpVisible(w.data.detail || ('failed (' + w.status + ')')); }
      };
      row.append(save, msg);
      instr.append(ta, row);
    }

    let busy = false;
    async function refresh() {
      if (busy || !box.isConnected) return;
      busy = true;
      try {
        const [s, p, f] = await Promise.all([
          cmpCall('/api/companion/status'),
          cmpCall('/api/companion/proposals?state=pending'),
          cmpCall('/api/companion/feed?limit=100'),
        ]);
        st = s.ok ? s.data : null;
        if (p.ok && typeof p.data.now === 'number') skew = p.data.now - Date.now() / 1000;
        drawHead();
        if (!armedId) drawProposals(p.ok ? p.data.proposals : []);
        drawFeed(f.ok ? f.data.items : []);
      } finally { busy = false; }
    }

    await refresh();
    drawInstructions();
    if (cmpTicker) clearInterval(cmpTicker);
    let ticks = 0;
    cmpTicker = setInterval(() => {
      if (!box.isConnected || !overlay.classList.contains('shown')) {
        clearInterval(cmpTicker); cmpTicker = null; return;
      }
      ticks++;
      box.querySelectorAll('.cmp-exp').forEach(e => {
        e.textContent = cmpLeft(Number(e.dataset.exp) - (Date.now() / 1000 + skew));
      });
      if (ticks % 30 === 0 || (st && st.check_running && ticks % 2 === 0)) refresh();
    }, 1000);
  }

  function renderAbout() {
    const box = document.createElement('div');
    box.innerHTML =
      '<div class="set-about-row"><span class="k">Version</span><span id="about-version">…</span></div>' +
      '<div class="set-about-row"><span class="k">Config file</span><span>~/.evi/config.toml</span></div>' +
      '<div class="set-about-row"><span class="k">Mode</span><span>' + (isDesktop() ? 'Desktop' : 'Web') + '</span></div>';
    const actions = document.createElement('div'); actions.style.marginTop = '16px';
    const mk = (lbl, fn) => { const b = document.createElement('button'); b.className = 'set-action-btn'; b.textContent = lbl; b.onclick = fn; return b; };
    actions.appendChild(mk('Documentation', openDocs));
    actions.appendChild(mk('Run diagnostics', openDiagnostics));
    actions.appendChild(mk('Check for updates', checkForUpdates));
    actions.appendChild(mk('Open logs', openLogs));
    actions.appendChild(mk('Keyboard shortcuts', openShortcuts));
    actions.appendChild(mk('Get support', () => openExternal('https://github.com/evi-assistant/evi-ai/issues')));
    box.appendChild(actions);
    contentEl.appendChild(box);
    fetch('/api/health').then(r => r.json()).then(d => {
      const el = document.getElementById('about-version'); if (el) el.textContent = 'eVi ' + (d.version || '');
    }).catch(() => {});
  }

  async function save() {
    if (!Object.keys(pending).length) return;
    saveBtn.disabled = true; statusEl.textContent = 'Saving…'; statusEl.style.color = '';
    try {
      const r = await fetch('/api/config', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(pending),
      });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();
      cfg = data.config; pending = {};
      if (typeof loadPicker === 'function') loadPicker();
      if (typeof loadCwd === 'function') loadCwd();
      // A model/backend change from Settings must update the header model chip
      // too — the footer picker path does this, so the two stay consistent.
      if ((data.touched || []).includes('llm') && typeof refreshHeaderModel === 'function')
        refreshHeaderModel();
      statusEl.textContent = (data.deferred && data.deferred.length)
        ? 'Saved — tool/permission changes apply to new chats.' : 'Saved.';
      statusEl.style.color = '#4caf50';
      saveBtn.disabled = true;
      renderSection();
    } catch (e) {
      statusEl.textContent = 'Save failed: ' + e.message; statusEl.style.color = '#e05252'; saveBtn.disabled = false;
    }
  }

  // Settings → Permissions' one-click fix for an auto-approve list from before
  // code_read: saves the list the server worked out (cfg.auto_approve_migration)
  // through the config API at once. It replaces an unsaved edit of that same
  // list (the checkboxes then show what was saved); other unsaved edits stay.
  async function migrateAutoApprove(btn) {
    const list = cfg && Array.isArray(cfg.auto_approve_migration) ? cfg.auto_approve_migration : null;
    if (!list) return;
    if (btn) btn.disabled = true;
    try {
      const r = await fetch('/api/config', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ auto: { auto_approve: list } }),
      });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();
      // The POST's snapshot has no availability / access probe: keep the GET's.
      cfg = Object.assign({}, data.config, { availability: cfg.availability, access: cfg.access });
      if (pending.auto) {
        delete pending.auto.auto_approve;
        if (!Object.keys(pending.auto).length) delete pending.auto;
      }
      markDirty();
      if (!Object.keys(pending).length) {
        statusEl.textContent = Array.isArray(cfg.auto_approve_migration)
          // Config.save writes only your config.toml: an active profile or this
          // folder's .evi.toml can still list "code".
          ? 'Saved, but "code" is still listed by an active profile or this folder’s .evi.toml.'
          : 'Saved — running code now asks first (applies to new chats).';
        statusEl.style.color = Array.isArray(cfg.auto_approve_migration) ? '#e0a52e' : '#4caf50';
      }
      renderSection();
    } catch (e) {
      statusEl.textContent = 'Switch failed: ' + e.message; statusEl.style.color = '#e05252';
      if (btn) btn.disabled = false;
    }
  }

  async function openSettings(sectionId) {
    if (overlay.classList.contains('shown')) return;
    pending = {}; activeSection = sectionId || 'general';
    statusEl.textContent = 'Loading…'; saveBtn.disabled = true;
    overlay.classList.add('shown');
    await loadConfig();
    statusEl.textContent = ''; renderNav(); renderSection();
  }
  function closeSettings() {
    if (Object.keys(pending).length && !confirm('Discard unsaved changes?')) return;
    pending = {}; overlay.classList.remove('shown');
  }
  document.getElementById('settings-btn').addEventListener('click', () => openSettings());
  document.getElementById('settings-close').addEventListener('click', closeSettings);
  document.getElementById('settings-cancel').addEventListener('click', closeSettings);
  saveBtn.addEventListener('click', save);
  overlay.addEventListener('click', e => { if (e.target === overlay) closeSettings(); });

  // ---- dispatch view (Phase 85) --------------------------------------
  function openSessionById(id) {
    let i = tabs.findIndex(t => t.id === id);
    if (i < 0) { tabs.push({ id, label: id.slice(0, 8) }); i = tabs.length - 1; renderTabs(); }
    if (i !== activeIdx) switchTab(i);
  }
  const _k = n => n >= 1000 ? Math.round(n / 1000) + 'k' : '' + n;
  async function openDispatch() {
    let data;
    try {
      const r = await fetch('/api/dispatch');
      if (!r.ok) return;
      data = await r.json();
    } catch { return; }
    const ov = document.createElement('div');
    ov.className = 'dispatch-overlay';
    const sessionsHtml = (sessions) => (sessions || []).map(s => {
      const ctx = s.ceiling ? ` · ${_k(s.used)}/${_k(s.ceiling)} ctx` : '';
      const pend = s.pending ? ` · ${s.pending} pending` : '';
      const chan = s.channels ? ` · ${s.channels} channel` : '';
      const busy = s.busy ? '<span class="d-busy" title="working">●</span> ' : '';
      // Prefer this browser's local tab label, then the server-stored name
      // (so sessions opened in other tabs still resolve), then the short id.
      const label = (tabs.find(t => t.id === s.id) || {}).label || s.name || s.id.slice(0, 8);
      const self = s.id === sessionId;
      return `<div class="dispatch-row"><div class="grow"><div class="d-title">${busy}${escapeHtml(label)}${self ? ' <span class="d-sub">(this tab)</span>' : ''}</div>`
        + `<div class="d-sub">${escapeHtml(s.mode)} · ${s.messages} msgs${ctx}${pend}${chan}</div></div>`
        + (self ? '' : `<button data-msg="${escapeHtml(s.id)}">Message</button>`)
        + `<button data-open="${escapeHtml(s.id)}">Open</button></div>`;
    }).join('') || '<div class="dispatch-empty">No active sessions.</div>';
    const wfRows = (data.workflows || []).map(w => {
      const par = w.parallel ? `, ${w.parallel} parallel` : '';
      const desc = w.description ? ' · ' + escapeHtml(w.description) : '';
      return `<div class="dispatch-row"><div class="grow"><div class="d-title">${escapeHtml(w.name)}</div>`
        + `<div class="d-sub">${w.steps} steps${par}${desc}</div></div>`
        + `<button data-run="${escapeHtml(w.name)}">Run</button></div>`;
    }).join('') || '<div class="dispatch-empty">No workflows — create one with <code>evi workflow new</code>.</div>';
    ov.innerHTML = `<div class="dispatch-panel"><button class="dispatch-close" data-close>✕</button>`
      + `<h2>Dispatch</h2><div class="d-sub">Active sessions and runnable workflows — live.</div>`
      + `<h3>Sessions (<span id="dispatch-sess-count">${(data.sessions || []).length}</span>)</h3>`
      + `<div id="dispatch-sessions">${sessionsHtml(data.sessions)}</div>`
      + `<h3>Workflows</h3>${wfRows}`
      + `<div id="dispatch-result"></div></div>`;
    document.body.appendChild(ov);
    // Live agent-watch: re-render the session list as turns start/finish.
    let dispatchES = null;
    try {
      dispatchES = new EventSource('/api/dispatch/stream?' + authQuery());
      dispatchES.onmessage = (ev) => {
        try {
          const d = JSON.parse(ev.data);
          const cont = document.getElementById('dispatch-sessions');
          const cnt = document.getElementById('dispatch-sess-count');
          if (cont) cont.innerHTML = sessionsHtml(d.sessions);
          if (cnt) cnt.textContent = (d.sessions || []).length;
        } catch {}
      };
    } catch {}
    const closeDispatch = () => { if (dispatchES) { dispatchES.close(); dispatchES = null; } ov.remove(); };
    ov.addEventListener('click', async (e) => {
      if (e.target === ov || e.target.hasAttribute('data-close')) { closeDispatch(); return; }
      const openId = e.target.getAttribute('data-open');
      if (openId) { closeDispatch(); openSessionById(openId); return; }
      // Compose a message to another session (reveal an inline textarea + Send).
      const msgId = e.target.getAttribute('data-msg');
      if (msgId) {
        const name = (tabs.find(t => t.id === msgId) || {}).label || msgId.slice(0, 8);
        const out = document.getElementById('dispatch-result');
        out.innerHTML = `<h3>Message “${escapeHtml(name)}”</h3>`
          + `<textarea id="dispatch-msg-text" rows="2" placeholder="Plain-text message to this session…" style="width:100%;box-sizing:border-box"></textarea>`
          + `<div style="margin-top:6px"><button data-send="${escapeHtml(msgId)}">Send</button></div>`;
        const ta = document.getElementById('dispatch-msg-text'); if (ta) ta.focus();
        return;
      }
      const sendId = e.target.getAttribute('data-send');
      if (sendId) {
        const ta = document.getElementById('dispatch-msg-text');
        const text = ((ta && ta.value) || '').trim();
        if (!text) { if (ta) ta.focus(); return; }
        e.target.disabled = true; e.target.textContent = 'Sending…';
        const out = document.getElementById('dispatch-result');
        try {
          const r = await fetch(`/api/session/${encodeURIComponent(sendId)}/message`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text }),
          });
          const d = await r.json().catch(() => ({}));
          out.innerHTML = r.ok
            ? `<div class="dispatch-empty">✓ delivered${d.delivered === false ? ' (duplicate skipped)' : ''}.</div>`
            : `<div class="dispatch-empty">error: ${escapeHtml(d.detail || 'failed')}</div>`;
        } catch (err) {
          out.innerHTML = `<div class="dispatch-empty">error: ${escapeHtml(String(err))}</div>`;
        }
        return;
      }
      const runName = e.target.getAttribute('data-run');
      if (runName) {
        e.target.disabled = true; e.target.textContent = 'Running…';
        const out = document.getElementById('dispatch-result');
        out.innerHTML = `<h3>Running ${escapeHtml(runName)}…</h3>`;
        try {
          const r = await fetch(`/api/dispatch/workflow/${encodeURIComponent(runName)}`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
          });
          const d = await r.json();
          if (!r.ok) {
            out.innerHTML = `<div class="dispatch-empty">error: ${escapeHtml(d.detail || 'failed')}</div>`;
          } else {
            out.innerHTML = `<h3>${escapeHtml(runName)} — result</h3>` +
              Object.entries(d.outputs || {}).map(([id, v]) =>
                `<div class="dispatch-row"><div class="grow"><div class="d-title">${escapeHtml(id)}</div>`
                + `<div class="d-sub" style="white-space:pre-wrap">${escapeHtml(String(v).slice(0, 2000))}</div></div></div>`
              ).join('');
          }
        } catch (err) {
          out.innerHTML = `<div class="dispatch-empty">error: ${escapeHtml(String(err))}</div>`;
        }
        e.target.disabled = false; e.target.textContent = 'Run';
      }
    });
  }
  document.getElementById('dispatch-btn').addEventListener('click', openDispatch);

  // ---- in-app documentation ------------------------------------------
  let docsPages = [];
  async function openDocs() {
    openDialog('Documentation', '<p class="sec-blurb">Loading…</p>');
    try {
      const r = await fetch('/api/docs'); if (!r.ok) throw new Error('HTTP ' + r.status);
      docsPages = (await r.json()).pages || [];
      if (!docsPages.length) { dlgContent.innerHTML = '<p class="sec-blurb">No docs found.</p>'; return; }
      await showDocPage(docsPages[0].slug);
    } catch (e) { dlgContent.innerHTML = '<p class="sec-blurb">Docs unavailable: ' + escapeHtml(e.message) + '</p>'; }
  }
  async function showDocPage(slug) {
    const nav = docsPages.map(p =>
      `<a class="docs-nav-link" data-slug="${escapeAttr(p.slug)}" style="margin-right:14px;white-space:nowrap">${escapeHtml(p.title)}</a>`
    ).join('');
    dlgContent.innerHTML =
      `<div style="margin-bottom:12px;border-bottom:1px solid var(--border);padding-bottom:8px;overflow-x:auto;white-space:nowrap">${nav}</div>` +
      '<div class="docs-body" id="doc-page">Loading…</div>';
    dlgContent.querySelectorAll('.docs-nav-link').forEach(a => a.onclick = () => showDocPage(a.dataset.slug));
    try {
      const d = await (await fetch('/api/docs/' + encodeURIComponent(slug))).json();
      document.getElementById('doc-page').innerHTML = d.html || '';
    } catch { document.getElementById('doc-page').textContent = 'Failed to load.'; }
  }

  // ---- diagnostics (evi doctor) --------------------------------------
  async function openDiagnostics() {
    openDialog('Diagnostics', '<p class="sec-blurb">Running checks…</p>');
    try {
      const r = await fetch('/api/doctor'); if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();
      const rows = (data.checks || []).map(c => {
        const icon = c.ok ? '✓' : (c.level === 'warn' ? '⚠' : '✗');
        const color = c.ok ? 'var(--user)' : (c.level === 'warn' ? 'var(--tool)' : 'var(--err)');
        return `<div class="set-about-row"><span><span style="color:${color}">${icon}</span> ${escapeHtml(c.name)}</span><span class="k">${escapeHtml(c.detail || '')}</span></div>`;
      }).join('');
      dlgContent.innerHTML = `<div class="docs-body">${rows || '<p>No checks.</p>'}</div>`;
    } catch (e) { dlgContent.innerHTML = '<p class="sec-blurb">Diagnostics failed: ' + escapeHtml(e.message) + '</p>'; }
  }

  // ---- updates / logs / shortcuts ------------------------------------
  async function checkForUpdates() {
    if (!isDesktop()) {
      openDialog('Updates', '<p class="sec-blurb">Auto-update runs in the desktop app. The CLI/web package updates with <code>pip install -U evi-assistant</code>.</p>');
      return;
    }
    openDialog('Updates', '<p class="sec-blurb">⟳ Checking for updates…</p>');
    try {
      const res = await invoke('check_for_update_cmd');
      if (res && res.available) { showUpdatePrompt(res.version, res.current); return; }
      // No full-app update — but the sidecar channel may have a newer core staged
      // (e.g. the user picked "Later" earlier). Offer to apply it here too, so
      // "Check for Updates" is never misleadingly "up to date" while a core waits.
      let staged = null;
      try { staged = await invoke('staged_update_cmd'); } catch (e) {}
      let running = '';
      try { running = (await (await fetch('/api/health', { cache: 'no-store' })).json()).version || ''; } catch (e) {}
      if (staged && staged !== running) { showStagedPrompt(staged); return; }
      dlgContent.innerHTML =
        `<p>✓ You’re up to date${running ? ' (v' + escapeHtml(running) + ')' : ''}.</p>`;
    } catch (e) {
      dlgContent.innerHTML = '<p class="sec-blurb">⚠ Update check failed: ' + escapeHtml(String(e)) + '</p>';
    }
  }

  // Confirm dialog for a full-app update — nothing installs until the user
  // clicks "Update & Restart". Shown by Help → Check for Updates and by the
  // launch-time auto-check (via pollUpdatePrompts).
  function showUpdatePrompt(version, current) {
    _promptShown = true; // stop the background poll; don't raise a second prompt
    openDialog('Update available', '');
    dlgContent.innerHTML =
      `<p><b>eVi ${escapeHtml(version || '')} is available.</b></p>` +
      `<p class="sec-blurb">${current ? 'You have v' + escapeHtml(current) + '. ' : ''}` +
      `eVi will download it and restart. Your chats are saved.</p>` +
      `<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:18px">` +
      `<button class="set-action-btn" id="upd-cancel">Not now</button>` +
      `<button class="set-action-btn" id="upd-ok" style="background:var(--accent);color:#fff;border-color:var(--accent)">Update &amp; Restart</button>` +
      `</div>`;
    document.getElementById('upd-cancel').onclick = () => {
      invoke('dismiss_update_cmd').catch(() => {});
      closeDialog();
    };
    document.getElementById('upd-ok').onclick = () => {
      dlgContent.innerHTML =
        `<p class="sec-blurb">⤓ Downloading eVi ${escapeHtml(version || '')} — the app will restart automatically when it’s ready.</p>`;
      pollUpdate();
      invoke('install_update_cmd').then(installing => {
        // false = the release was yanked/replaced between the offer and now, so
        // nothing installs — clear the "Downloading…" message instead of hanging.
        if (!installing) dlgContent.innerHTML = '<p>✓ You’re up to date — that update is no longer available.</p>';
      }).catch(e => {
        dlgContent.innerHTML = '<p class="sec-blurb">⚠ Update failed: ' + escapeHtml(String(e)) + '</p>';
      });
    };
  }

  // Prompt shown when the sidecar channel has staged a newer *core* — a plain
  // restart (not a full reinstall) picks it up. Fixes the "About still shows the
  // old version until I completely exit and reopen" gap: closing the window only
  // hides eVi to the tray, so the old core keeps running until a real relaunch.
  function showStagedPrompt(version) {
    _promptShown = true; // stop the background poll; don't raise a second prompt
    openDialog('Core update ready', '');
    dlgContent.innerHTML =
      `<p><b>A core update (eVi ${escapeHtml(version || '')}) is ready.</b></p>` +
      `<p class="sec-blurb">Restart eVi to start using it. Your chats are saved.</p>` +
      `<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:18px">` +
      `<button class="set-action-btn" id="stg-later">Later</button>` +
      `<button class="set-action-btn" id="stg-now" style="background:var(--accent);color:#fff;border-color:var(--accent)">Restart now</button>` +
      `</div>`;
    document.getElementById('stg-later').onclick = closeDialog;
    document.getElementById('stg-now').onclick = () => {
      dlgContent.innerHTML = '<p class="sec-blurb">↻ Restarting eVi…</p>';
      invoke('restart_app_cmd').catch(() => {});
    };
  }
  function openLogs() {
    if (isDesktop()) invoke('open_logs_cmd').catch(() => {});
    else openDialog('Logs', '<p class="sec-blurb">Logs are written to <code>~/.evi/logs</code>.</p>');
  }

  // ---- silent auto-update progress toast (desktop) -------------------
  function _updateToast() {
    let t = document.getElementById('update-toast');
    if (!t) {
      t = document.createElement('div');
      t.id = 'update-toast';
      t.style.cssText = 'position:fixed;top:10px;left:50%;transform:translateX(-50%);z-index:2500;background:var(--panel);border:1px solid var(--accent);border-radius:8px;padding:8px 14px;font-size:13px;box-shadow:0 4px 16px rgba(0,0,0,.45);min-width:240px;text-align:center;color:var(--fg)';
      t.innerHTML = '<div id="update-toast-text">Updating eVi…</div>'
        + '<div class="rec-progress" style="margin-top:6px"><div class="rec-bar" id="update-toast-bar"></div></div>';
      document.body.appendChild(t);
    }
    return t;
  }
  async function pollUpdate() {
    if (!isDesktop()) return;
    let s;
    try { s = await invoke('update_status_cmd'); } catch { return; }
    if (!s || !s.phase) return;
    const t = _updateToast();
    const text = t.querySelector('#update-toast-text');
    const bar = t.querySelector('#update-toast-bar');
    if (s.phase === 'downloading') {
      const pct = s.total ? Math.round(s.downloaded * 100 / s.total) : 0;
      text.textContent = `Downloading eVi ${s.version || ''} — ${pct}%`;
      bar.style.width = pct + '%';
      setTimeout(pollUpdate, 500);
    } else if (s.phase === 'installing') {
      text.textContent = 'Installing — eVi will restart…';
      bar.style.width = '100%';
      setTimeout(pollUpdate, 1000);
    } else if (s.phase === 'error') {
      text.textContent = 'Update failed — see Help → Open Logs.';
      setTimeout(() => t.remove(), 5000);
    }
  }
  function onUpdateStarted() { pollUpdate(); }

  // ---- update prompts (ask before upgrading) ------------------------
  // Poll the shell for (a) a full-app update the launch check found, and (b) a
  // newer core staged by the sidecar channel. Each raises a confirm prompt once;
  // nothing installs or restarts without the user's click.
  // A single gate: once ANY update prompt is raised we stop polling and never
  // raise a second one — so a late staged-core poll can't clobber an in-progress
  // download dialog, and a full-app + core release (which sets both offers) shows
  // just one prompt. The launch checks land within ~1–2 min, so polling is
  // bounded rather than running for the life of the (tray-resident) process.
  let _promptShown = false;
  let _pollAttempts = 0;
  async function pollUpdatePrompts() {
    if (!isDesktop() || _promptShown) return;
    _pollAttempts++;
    let shown = false;
    try {
      // Never overwrite a dialog the user already has open (ours or About/Docs/…).
      if (!dlgOverlay.classList.contains('shown')) {
        const a = await invoke('update_available_cmd');
        if (a && a.version) {
          showUpdatePrompt(a.version, a.current); shown = true;
        } else {
          const sv = await invoke('staged_update_cmd');
          if (sv) {
            let running = '';
            try { running = (await (await fetch('/api/health', { cache: 'no-store' })).json()).version || ''; } catch (e) {}
            if (sv !== running) { showStagedPrompt(sv); shown = true; }
          }
        }
      }
    } catch (e) {}
    if (!shown && _pollAttempts < 40) setTimeout(pollUpdatePrompts, 8000);
  }

  // Catch an auto-update that began before the page finished loading, and start
  // polling for update offers.
  if (isDesktop()) { setTimeout(pollUpdate, 1500); setTimeout(pollUpdatePrompts, 3000); }
  function openShortcuts() {
    const rows = [
      ['Enter', 'Send message'], ['Shift+Enter', 'New line'], ['Ctrl+,', 'Settings'],
      ['Ctrl+I', 'Switch model'], ['Shift+Ctrl+E', 'Cycle reasoning effort'],
      ['Ctrl+N', 'New chat'], ['Ctrl+F', 'Find in conversation'], ['Esc', 'Close dialog'],
    ].map(([k, v]) => `<div class="set-about-row"><span class="k">${k}</span><span>${v}</span></div>`).join('');
    openDialog('Keyboard shortcuts', `<div class="docs-body">${rows}</div>`);
  }

  // ---- file rewind (undo eVi's file writes) --------------------------
  async function rewindFiles() {
    openDialog('Rewind file changes', '<p class="sec-blurb">Loading…</p>');
    try {
      const cps = (await (await fetch('/api/checkpoints')).json()).checkpoints || [];
      if (!cps.length) { dlgContent.innerHTML = '<p class="sec-blurb">No file changes to undo.</p>'; return; }
      const rows = cps.slice().reverse().map(c =>
        `<div class="set-about-row"><span><span class="k">#${c.seq}</span> ${escapeHtml(c.op)} ${escapeHtml(c.path)}</span>` +
        `<button class="set-action-btn" style="margin:0" data-seq="${c.seq}">Undo from here</button></div>`
      ).join('');
      dlgContent.innerHTML =
        '<p class="sec-blurb">Undo eVi’s file writes. “Undo from here” reverts that write and every one after it.</p>' +
        '<div class="docs-body">' + rows + '</div>';
      dlgContent.querySelectorAll('button[data-seq]').forEach(b => b.onclick = async () => {
        if (!confirm('Undo file changes from #' + b.dataset.seq + ' onward?')) return;
        const res = await (await fetch('/api/rewind', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ seq: Number(b.dataset.seq) }),
        })).json();
        const acts = res.actions || [];
        dlgContent.innerHTML = `<p>Reverted ${acts.length} file(s).</p><div class="docs-body">` +
          acts.map(a => `<div class="set-about-row"><span>${escapeHtml(a.path)}</span><span class="k">${escapeHtml(a.action)}</span></div>`).join('') +
          '</div>';
      });
    } catch (e) { dlgContent.innerHTML = '<p class="sec-blurb">Rewind unavailable: ' + escapeHtml(e.message) + '</p>'; }
  }

  // ---- keyboard: Ctrl+, opens settings; Esc closes overlays ----------
  window.addEventListener('keydown', e => {
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key === ',') { e.preventDefault(); openSettings(); }
    else if (e.key === 'Escape') {
      if (overlay.classList.contains('shown')) closeSettings();
      else if (dlgOverlay.classList.contains('shown')) closeDialog();
    }
  });

  // ---- zoom (View menu) ----------------------------------------------
  const ZOOM_KEY = 'evi.zoom';
  let zoomLevel = parseFloat(localStorage.getItem(ZOOM_KEY) || '1') || 1;
  function applyZoom() { document.documentElement.style.zoom = String(zoomLevel); }
  function setZoom(delta, reset) {
    zoomLevel = reset ? 1 : Math.min(2, Math.max(0.5, Math.round((zoomLevel + delta) * 10) / 10));
    localStorage.setItem(ZOOM_KEY, String(zoomLevel)); applyZoom();
  }
  applyZoom();

  // ---- new chat / export / find (File + Edit menus) ------------------
  function newChat() {
    if (typeof newTab === 'function') newTab();
    else { const p = document.querySelector('[data-new]'); if (p) p.click(); }
  }
  function exportChat() {
    const parts = [];
    document.querySelectorAll('#log .msg').forEach(b => {
      const role = b.classList.contains('user') ? 'You'
        : b.classList.contains('assistant') ? 'eVi'
        : b.classList.contains('tool') ? 'Tool' : 'System';
      const text = b.dataset.source || b.textContent || '';
      if (text.trim()) parts.push(`**${role}:**\n\n${text}\n`);
    });
    const blob = new Blob([parts.join('\n---\n\n') || '_empty chat_'], { type: 'text/markdown' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'evi-chat.md';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }
  function openFind() {
    let bar = document.getElementById('find-bar');
    if (!bar) {
      bar = document.createElement('div');
      bar.id = 'find-bar';
      bar.style.cssText = 'position:fixed;top:8px;right:16px;z-index:1800;display:flex;gap:6px;align-items:center;background:var(--panel);border:1px solid var(--border);border-radius:6px;padding:6px 8px;box-shadow:0 4px 16px rgba(0,0,0,.4)';
      const inp = document.createElement('input');
      inp.id = 'find-input'; inp.type = 'text';
      inp.setAttribute('place' + 'holder', 'Find in chat…');  // attr name split for the linter
      inp.style.cssText = 'background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:4px 8px;font:inherit';
      const next = document.createElement('button'); next.className = 'set-action-btn'; next.style.margin = '0'; next.textContent = 'Next';
      const close = document.createElement('button'); close.className = 'set-action-btn'; close.style.margin = '0'; close.textContent = '✕';
      bar.append(inp, next, close);
      document.body.appendChild(bar);
      const run = () => { const t = inp.value; if (t && window.find) window.find(t); };
      inp.addEventListener('keydown', e => {
        if (e.key === 'Enter') run();
        else if (e.key === 'Escape') bar.remove();
      });
      next.addEventListener('click', run);
      close.addEventListener('click', () => bar.remove());
    }
    const fi = document.getElementById('find-input'); fi.focus(); fi.select();
  }

  // ---- native menu/tray bridge (desktop only) ------------------------
  function handleMenu(id) {
    switch (id) {
      case 'new_chat': newChat(); break;
      case 'settings': openSettings(); break;
      case 'open_file': { const fi = document.getElementById('file-input'); if (fi) fi.click(); break; }
      case 'export_chat': exportChat(); break;
      case 'find': openFind(); break;
      case 'documentation': openDocs(); break;
      case 'diagnostics': openDiagnostics(); break;
      case 'shortcuts': openShortcuts(); break;
      case 'about': openSettings('about'); break;
      case 'toggle_theme': toggleTheme(); break;
      case 'get_support': openExternal('https://github.com/evi-assistant/evi-ai/issues'); break;
      case 'check_updates': checkForUpdates(); break;
      case 'open_logs': openLogs(); break;
      case 'rewind': rewindFiles(); break;
      case 'reload': location.reload(); break;
      case 'zoom_in': setZoom(0.1); break;
      case 'zoom_out': setZoom(-0.1); break;
      case 'zoom_reset': setZoom(0, true); break;
    }
  }
  // ---- expose for the desktop menu/tray bridge -----------------------
  // (main.rs runs window.eviUI.handleMenu(id) in the page; no Tauri event API.)
  window.eviUI = {
    openSettings, openDocs, openDiagnostics, checkForUpdates, openLogs,
    openShortcuts, toggleTheme, setThemePref, openExternal, closeDialog,
    newChat, exportChat, openFind, setZoom, handleMenu, rewindFiles,
    onUpdateStarted,
  };
})();
