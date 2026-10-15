// eVi for VS Code — a thin client over the local eVi server.
//   - Inline (ghost-text) completions  -> POST {serverUrl}/api/complete  (FIM)
//   - Chat sidebar (own webview, no Copilot dependency) -> POST /api/chat (SSE)
// No Python changes: eVi already exposes both endpoints. Local-first; nothing
// leaves your machine.

import { randomBytes } from 'crypto';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import * as vscode from 'vscode';

function cfg() {
  return vscode.workspace.getConfiguration('evi');
}
function serverUrl(): string {
  return (cfg().get<string>('serverUrl') || 'http://127.0.0.1:8473').replace(/\/+$/, '');
}
// The desktop app's generated access token (~/.evi/desktop-token), which its
// server requires while LAN access is on. Read only for a server on this
// machine, so it is never sent anywhere else.
function desktopToken(): string {
  try {
    const host = new URL(serverUrl()).hostname;
    if (!['127.0.0.1', 'localhost', '[::1]', '::1'].includes(host)) return '';
    const home = process.env.EVI_HOME || path.join(os.homedir(), '.evi');
    const tok = fs.readFileSync(path.join(home, 'desktop-token'), 'utf8').trim();
    return /^[0-9a-f]{32,128}$/i.test(tok) ? tok : '';
  } catch {
    return '';
  }
}
function authHeaders(): Record<string, string> {
  const t = cfg().get<string>('authToken') || desktopToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
}
// Every request names its client; with no token the server requires this on a
// browser's state-changing requests. The extension host's fetch (Node's undici)
// sends `Sec-Fetch-Mode: cors` but no Origin or Sec-Fetch-Site, so the server
// doesn't count it as a browser — the header is for symmetry and
// future-proofing.
function clientHeaders(): Record<string, string> {
  return { 'X-Evi-Client': 'vscode', ...authHeaders() };
}

// ---- inline (ghost-text) completion provider ----------------------------

class EviInlineProvider implements vscode.InlineCompletionItemProvider {
  async provideInlineCompletionItems(
    document: vscode.TextDocument,
    position: vscode.Position,
    context: vscode.InlineCompletionContext,
    token: vscode.CancellationToken,
  ): Promise<vscode.InlineCompletionItem[] | undefined> {
    if (!cfg().get<boolean>('autocomplete.enabled')) return;
    // Don't fight the normal IntelliSense popup.
    if (context.selectedCompletionInfo) return;

    // Debounce: wait for a typing pause; bail if superseded/cancelled.
    const debounce = cfg().get<number>('debounceMs') ?? 250;
    await new Promise((r) => setTimeout(r, debounce));
    if (token.isCancellationRequested) return;

    // Build prefix/suffix around the cursor (bounded for latency).
    const full = document.getText();
    const offset = document.offsetAt(position);
    const MAX = 8000;
    const prefix = full.slice(Math.max(0, offset - MAX), offset);
    const suffix = full.slice(offset, offset + MAX);

    const controller = new AbortController();
    token.onCancellationRequested(() => controller.abort());
    let completion = '';
    try {
      const resp = await fetch(`${serverUrl()}/api/complete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...clientHeaders() },
        body: JSON.stringify({
          prefix,
          suffix,
          model: cfg().get<string>('completionModel') || '',
          max_tokens: cfg().get<number>('maxTokens') ?? 128,
        }),
        signal: controller.signal,
      });
      if (!resp.ok) return;
      completion = ((await resp.json()) as { completion?: string }).completion || '';
    } catch {
      return; // server down / aborted — stay quiet
    }
    if (!completion || token.isCancellationRequested) return;
    return [new vscode.InlineCompletionItem(completion, new vscode.Range(position, position))];
  }
}

// ---- chat sidebar (own webview; streams /api/chat) ----------------------

class EviChatView implements vscode.WebviewViewProvider {
  // One server session per window (kept across reloads in workspaceState):
  // a single shared id made every window race the same agent.
  constructor(private readonly extUri: vscode.Uri, private readonly sessionId: string) {}

  resolveWebviewView(view: vscode.WebviewView): void {
    view.webview.options = { enableScripts: true };
    view.webview.html = this.html();
    view.webview.onDidReceiveMessage(async (msg) => {
      if (msg?.type === 'send') {
        await this.stream(view, String(msg.text || ''));
      } else if (msg?.type === 'decide') {
        await this.decide(String(msg.id || ''), msg.approved === true);
      }
    });
  }

  seed(view: vscode.WebviewView, text: string) {
    view.webview.postMessage({ type: 'seed', text });
  }

  // Answer a PermissionRequest (the webview's Approve / Deny buttons).
  private async decide(decisionId: string, approved: boolean) {
    if (!decisionId) return;
    try {
      await fetch(`${serverUrl()}/api/decide`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...clientHeaders() },
        body: JSON.stringify({ session_id: this.sessionId, decision_id: decisionId, approved }),
      });
    } catch {
      /* the server denies at its deadline anyway */
    }
  }

  private async stream(view: vscode.WebviewView, text: string) {
    const post = (m: Record<string, unknown>) => view.webview.postMessage(m);
    try {
      const resp = await fetch(`${serverUrl()}/api/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...clientHeaders() },
        body: JSON.stringify({ session_id: this.sessionId, message: text }),
      });
      if (resp.status === 409) {
        post({ type: 'token', text: '\n[eVi is still working on the previous message — try again when it finishes]' });
        post({ type: 'done' });
        return;
      }
      if (!resp.ok || !resp.body) {
        post({ type: 'token', text: `\n[eVi error: HTTP ${resp.status}]` });
        post({ type: 'done' });
        return;
      }
      // SSE: lines like `data: {json}`.
      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const parts = buf.split('\n');
        buf = parts.pop() || '';
        for (const line of parts) {
          const s = line.trim();
          if (!s.startsWith('data:')) continue;
          let ev: { [k: string]: unknown } | null = null;
          try {
            ev = JSON.parse(s.slice(5).trim());
          } catch {
            /* ignore keepalives / non-json */
          }
          if (!ev || typeof ev !== 'object') continue;
          if (ev.kind === 'TextDelta' && typeof ev.text === 'string' && ev.text) {
            post({ type: 'token', text: ev.text });
          } else if (ev.kind === 'PermissionRequest') {
            // A tool needs approval: unanswered, the turn would wait until the
            // server's deadline denies it.
            post({
              type: 'permission',
              id: String(ev.decision_id || ''),
              tool: String(ev.tool_name || ''),
              args: String(ev.args || ''),
              category: String(ev.category || ''),
              timeout: Number(ev.timeout_s) || 0,
            });
          } else if (ev.kind === 'PermissionExpired') {
            post({ type: 'expired', id: String(ev.decision_id || '') });
          } else if (ev.kind === 'Error') {
            post({ type: 'token', text: `\n[eVi error: ${String(ev.message || 'unknown error')}]` });
          }
        }
      }
    } catch (e) {
      post({ type: 'token', text: `\n[eVi error: ${e}]` });
    }
    post({ type: 'done' });
  }

  private html(): string {
    return /* html */ `<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
  body { font-family: var(--vscode-font-family); margin: 0; padding: 8px; color: var(--vscode-foreground); }
  #log { white-space: pre-wrap; font-size: 13px; }
  .u { color: var(--vscode-textLink-foreground); margin-top: 10px; }
  #row { display: flex; gap: 6px; margin-top: 8px; position: sticky; bottom: 0; }
  #q { flex: 1; background: var(--vscode-input-background); color: var(--vscode-input-foreground);
       border: 1px solid var(--vscode-input-border); padding: 6px; }
  button { background: var(--vscode-button-background); color: var(--vscode-button-foreground); border: 0; padding: 6px 10px; }
  button:disabled { opacity: .5; }
  .perm { border: 1px solid var(--vscode-input-border); padding: 6px; margin-top: 8px; }
  .perm pre { white-space: pre-wrap; max-height: 8em; overflow: auto; margin: 4px 0; }
  .perm button { margin-right: 6px; }
</style></head><body>
<div id="log"></div>
<div id="row"><input id="q" placeholder="Ask eVi…" /><button id="send">Send</button></div>
<script>
  const vscodeApi = acquireVsCodeApi();
  const log = document.getElementById('log'), q = document.getElementById('q');
  let cur = null;
  const perms = {};  // decision id -> settle(label)
  function add(cls, t){ const d=document.createElement('div'); d.className=cls; d.textContent=t; log.appendChild(d); d.scrollIntoView(); return d; }
  function send(){ const t=q.value.trim(); if(!t) return; add('u','you: '+t); q.value=''; cur=add('a','eVi: '); vscodeApi.postMessage({type:'send', text:t}); }
  // A tool asks for approval: Approve / Deny, or the server's deadline denies.
  function permBox(m){
    const box=document.createElement('div'); box.className='perm';
    const h=document.createElement('div'); h.textContent='Allow '+m.tool+(m.category?' ('+m.category+')':'')+'?';
    const a=document.createElement('pre'); a.textContent=m.args;
    const yes=document.createElement('button'); yes.textContent='Approve';
    const no=document.createElement('button'); no.textContent='Deny';
    const note=document.createElement('span');
    let timer=null;
    function settle(label){ yes.disabled=true; no.disabled=true; note.textContent=' '+label; delete perms[m.id]; if(timer) clearTimeout(timer); }
    yes.onclick=()=>{ settle('approved'); vscodeApi.postMessage({type:'decide', id:m.id, approved:true}); };
    no.onclick=()=>{ settle('denied'); vscodeApi.postMessage({type:'decide', id:m.id, approved:false}); };
    if(m.timeout>0) timer=setTimeout(()=>settle('expired, denied'), m.timeout*1000);
    box.appendChild(h); box.appendChild(a); box.appendChild(yes); box.appendChild(no); box.appendChild(note);
    log.appendChild(box); box.scrollIntoView();
    perms[m.id]=settle;
    cur=null;  // the reply continues below the prompt
  }
  document.getElementById('send').onclick=send;
  q.addEventListener('keydown', e=>{ if(e.key==='Enter') send(); });
  window.addEventListener('message', e=>{ const m=e.data;
    if(m.type==='token'){ if(!cur) cur=add('a','eVi: '); cur.textContent+=m.text; cur.scrollIntoView(); }
    else if(m.type==='permission'){ permBox(m); }
    else if(m.type==='expired'){ if(perms[m.id]) perms[m.id]('expired, denied'); }
    else if(m.type==='done'){ cur=null; }
    else if(m.type==='seed'){ q.value=m.text; q.focus(); }
  });
</script></body></html>`;
  }
}

// ---- activation ---------------------------------------------------------

// This window's chat session id, stable across reloads of the same workspace.
function windowSessionId(ctx: vscode.ExtensionContext): string {
  const key = 'evi.sessionId';
  let sid = ctx.workspaceState.get<string>(key) || '';
  if (!/^vscode-[0-9a-f]{16}$/.test(sid)) {
    sid = `vscode-${randomBytes(8).toString('hex')}`;
    void ctx.workspaceState.update(key, sid);
  }
  return sid;
}

export function activate(ctx: vscode.ExtensionContext) {
  const chat = new EviChatView(ctx.extensionUri, windowSessionId(ctx));
  let chatView: vscode.WebviewView | undefined;
  const origResolve = chat.resolveWebviewView.bind(chat);
  chat.resolveWebviewView = (v: vscode.WebviewView) => { chatView = v; origResolve(v); };

  ctx.subscriptions.push(
    vscode.languages.registerInlineCompletionItemProvider({ pattern: '**' }, new EviInlineProvider()),
    vscode.window.registerWebviewViewProvider('evi.chat', chat),
    vscode.commands.registerCommand('evi.toggleAutocomplete', async () => {
      const c = cfg();
      const now = !c.get<boolean>('autocomplete.enabled');
      await c.update('autocomplete.enabled', now, vscode.ConfigurationTarget.Global);
      vscode.window.showInformationMessage(`eVi autocomplete ${now ? 'on' : 'off'}`);
    }),
    vscode.commands.registerCommand('evi.openChat', () => vscode.commands.executeCommand('evi.chat.focus')),
    vscode.commands.registerCommand('evi.explainSelection', async () => {
      const ed = vscode.window.activeTextEditor;
      if (!ed) return;
      const sel = ed.document.getText(ed.selection) || ed.document.getText();
      await vscode.commands.executeCommand('evi.chat.focus');
      const prompt = `Explain this code from ${ed.document.fileName}:\n\n${sel}`;
      if (chatView) chat.seed(chatView, prompt);
    }),
  );

  // Status bar: reachability + autocomplete state + completion model.
  // Click toggles autocomplete; tooltip shows server/model details.
  const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  status.command = 'evi.toggleAutocomplete';
  status.show();
  ctx.subscriptions.push(status);

  let reachable = false;
  let model = '';
  let warnedDown = false;

  async function refreshStatus() {
    const auto = cfg().get<boolean>('autocomplete.enabled') ? 'auto on' : 'auto off';
    try {
      const r = await fetch(`${serverUrl()}/api/health`, { headers: authHeaders() });
      reachable = r.ok;
    } catch {
      reachable = false;
    }
    if (reachable) {
      try {
        const p = await fetch(`${serverUrl()}/api/model-picker`, { headers: authHeaders() });
        if (p.ok) model = ((await p.json()) as { active?: string }).active || '';
      } catch { /* keep last */ }
      status.text = `$(check) eVi · ${auto}`;
      status.tooltip = `eVi local copilot — reachable at ${serverUrl()}\nmodel: ${model || '?'}\nclick to toggle autocomplete`;
      status.backgroundColor = undefined;
      warnedDown = false;
    } else {
      status.text = `$(warning) eVi · offline`;
      status.tooltip = `eVi server not reachable at ${serverUrl()} — run \`evi web\` (or open the desktop app).`;
      status.backgroundColor = new vscode.ThemeColor('statusBarItem.warningBackground');
      if (!warnedDown) {
        warnedDown = true;
        vscode.window.showWarningMessage(
          `eVi: can't reach the server at ${serverUrl()}. Start it with "evi web" (or the desktop app), then completions + chat will work.`,
          'Open Settings',
        ).then((pick) => {
          if (pick === 'Open Settings')
            vscode.commands.executeCommand('workbench.action.openSettings', 'evi.serverUrl');
        });
      }
    }
  }

  refreshStatus();
  const timer = setInterval(refreshStatus, 30_000);
  ctx.subscriptions.push({ dispose: () => clearInterval(timer) });
  ctx.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration('evi')) refreshStatus();
    }),
  );
}

export function deactivate() {}
