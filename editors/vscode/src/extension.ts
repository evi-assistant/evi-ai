// eVi for VS Code — a thin client over the local eVi server.
//   - Inline (ghost-text) completions  -> POST {serverUrl}/api/complete  (FIM)
//   - Chat sidebar (own webview, no Copilot dependency) -> POST /api/chat (SSE)
// No Python changes: eVi already exposes both endpoints. Local-first; nothing
// leaves your machine.

import * as vscode from 'vscode';

function cfg() {
  return vscode.workspace.getConfiguration('evi');
}
function serverUrl(): string {
  return (cfg().get<string>('serverUrl') || 'http://127.0.0.1:8473').replace(/\/+$/, '');
}
function authHeaders(): Record<string, string> {
  const t = cfg().get<string>('authToken') || '';
  return t ? { Authorization: `Bearer ${t}` } : {};
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
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
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
  constructor(private readonly extUri: vscode.Uri) {}

  resolveWebviewView(view: vscode.WebviewView): void {
    view.webview.options = { enableScripts: true };
    view.webview.html = this.html();
    view.webview.onDidReceiveMessage(async (msg) => {
      if (msg?.type !== 'send') return;
      await this.stream(view, String(msg.text || ''));
    });
  }

  seed(view: vscode.WebviewView, text: string) {
    view.webview.postMessage({ type: 'seed', text });
  }

  private async stream(view: vscode.WebviewView, text: string) {
    const sid = 'vscode';
    try {
      const resp = await fetch(`${serverUrl()}/api/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ session_id: sid, message: text }),
      });
      if (!resp.ok || !resp.body) {
        view.webview.postMessage({ type: 'token', text: `\n[eVi error: HTTP ${resp.status}]` });
        view.webview.postMessage({ type: 'done' });
        return;
      }
      // SSE: lines like `data: {json}`. Parse TextDelta events.
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
          try {
            const ev = JSON.parse(s.slice(5).trim());
            if (ev.kind === 'TextDelta' && ev.text) {
              view.webview.postMessage({ type: 'token', text: ev.text });
            }
          } catch {
            /* ignore keepalives / non-json */
          }
        }
      }
    } catch (e) {
      view.webview.postMessage({ type: 'token', text: `\n[eVi error: ${e}]` });
    }
    view.webview.postMessage({ type: 'done' });
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
</style></head><body>
<div id="log"></div>
<div id="row"><input id="q" placeholder="Ask eVi…" /><button id="send">Send</button></div>
<script>
  const vscodeApi = acquireVsCodeApi();
  const log = document.getElementById('log'), q = document.getElementById('q');
  let cur = null;
  function add(cls, t){ const d=document.createElement('div'); d.className=cls; d.textContent=t; log.appendChild(d); d.scrollIntoView(); return d; }
  function send(){ const t=q.value.trim(); if(!t) return; add('u','you: '+t); q.value=''; cur=add('a','eVi: '); vscodeApi.postMessage({type:'send', text:t}); }
  document.getElementById('send').onclick=send;
  q.addEventListener('keydown', e=>{ if(e.key==='Enter') send(); });
  window.addEventListener('message', e=>{ const m=e.data;
    if(m.type==='token'){ if(!cur) cur=add('a','eVi: '); cur.textContent+=m.text; cur.scrollIntoView(); }
    else if(m.type==='done'){ cur=null; }
    else if(m.type==='seed'){ q.value=m.text; q.focus(); }
  });
</script></body></html>`;
  }
}

// ---- activation ---------------------------------------------------------

export function activate(ctx: vscode.ExtensionContext) {
  const chat = new EviChatView(ctx.extensionUri);
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
