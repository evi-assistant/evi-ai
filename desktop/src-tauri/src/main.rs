// Prevents the extra console window on Windows release builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use tauri::menu::{MenuBuilder, MenuItemBuilder, PredefinedMenuItem, SubmenuBuilder};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent};

mod sidecar_update;

/// Wraps the spawned Python server process so we can kill it on shutdown.
struct ServerHandle(Mutex<Option<Child>>);

/// Reserve an unused TCP port by binding to :0 and reading the assigned port.
/// The listener is dropped immediately so the OS frees the port for the child.
fn pick_free_port() -> Result<u16, std::io::Error> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let port = listener.local_addr()?.port();
    drop(listener);
    Ok(port)
}

/// Prefer a STABLE port so the webview origin (http://127.0.0.1:<port>) — and
/// thus its localStorage (open tabs + session ids) — survives an app restart.
/// Falls back to any free port if the preferred one is taken (e.g. a second
/// eVi instance), accepting that that launch starts with a fresh origin.
fn pick_port() -> Result<u16, std::io::Error> {
    const PREFERRED: u16 = 8473;
    if let Ok(listener) = TcpListener::bind(("127.0.0.1", PREFERRED)) {
        drop(listener);
        return Ok(PREFERRED);
    }
    pick_free_port()
}

/// Walk up from `start` looking for a directory containing `pyproject.toml`.
/// Falls back to None if not found — caller should warn and exit cleanly.
fn find_repo_root(start: &Path) -> Option<PathBuf> {
    let mut cur = Some(start);
    while let Some(dir) = cur {
        if dir.join("pyproject.toml").is_file() {
            return Some(dir.to_path_buf());
        }
        cur = dir.parent();
    }
    None
}

/// The eVi home dir: `EVI_HOME`, else `~/.evi`.
fn evi_home() -> Option<PathBuf> {
    std::env::var("EVI_HOME").ok().map(PathBuf::from).or_else(|| {
        std::env::var(if cfg!(windows) { "USERPROFILE" } else { "HOME" })
            .ok()
            .map(|h| PathBuf::from(h).join(".evi"))
    })
}

/// Env var the sidecar reads its launch token from (`evi/apps/web/server.py`
/// `_LAUNCH_TOKEN_ENV`). Part of the shell↔sidecar handshake: see SHELL_ABI.
const TOKEN_ENV: &str = "EVI_WEB_AUTH_TOKEN";

/// The desktop's generated LAN access token, under the eVi home (user-only
/// permissions on Unix; on Windows the profile directory's ACL already limits
/// it to the user). The VS Code extension and `evi` read it from here too.
const DESKTOP_TOKEN_FILE: &str = "desktop-token";

/// `<evi_home>/config.toml`, parsed — None when absent or unparsable.
fn read_config(home: &Path) -> Option<toml::Value> {
    std::fs::read_to_string(home.join("config.toml"))
        .ok()
        .and_then(|t| t.parse::<toml::Value>().ok())
}

/// A non-empty string at `[section] key`.
fn config_str(cfg: Option<&toml::Value>, section: &str, key: &str) -> Option<String> {
    cfg?.get(section)?
        .get(key)?
        .as_str()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
}

/// `[federation] bind_lan` — serve on 0.0.0.0 so this machine can be a peer.
fn config_bind_lan(cfg: Option<&toml::Value>) -> bool {
    cfg.and_then(|v| v.get("federation"))
        .and_then(|f| f.get("bind_lan"))
        .and_then(|b| b.as_bool())
        .unwrap_or(false)
}

/// 32 bytes from the OS CSPRNG, hex-encoded.
fn generate_token() -> Result<String, String> {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).map_err(|e| format!("no OS randomness: {e}"))?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}

/// Only a token this shell could have written is reused; anything else in the
/// file is replaced.
fn valid_token(t: &str) -> bool {
    t.len() == 64 && t.bytes().all(|b| b.is_ascii_hexdigit())
}

/// Write `contents` readable by this user only (0600 on Unix, set at creation
/// and again in case the file already existed with wider bits).
fn write_private(path: &Path, contents: &str) -> std::io::Result<()> {
    use std::io::Write;
    let mut opts = std::fs::OpenOptions::new();
    opts.write(true).create(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.mode(0o600);
    }
    let mut f = opts.open(path)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        f.set_permissions(std::fs::Permissions::from_mode(0o600))?;
    }
    f.write_all(contents.as_bytes())?;
    f.sync_all()
}

/// The desktop token: reuse `<home>/desktop-token`, or generate and persist one
/// on first need, so peers and the VS Code extension keep working across
/// launches.
fn load_or_create_token(home: &Path) -> Result<String, String> {
    let path = home.join(DESKTOP_TOKEN_FILE);
    if let Ok(existing) = std::fs::read_to_string(&path) {
        let existing = existing.trim();
        if valid_token(existing) {
            return Ok(existing.to_string());
        }
    }
    let token = generate_token()?;
    std::fs::create_dir_all(home).map_err(|e| e.to_string())?;
    write_private(&path, &token).map_err(|e| format!("{}: {e}", path.display()))?;
    Ok(token)
}

/// How to launch the local server, decided from config.toml.
#[derive(Debug, PartialEq, Eq)]
struct LaunchPlan {
    /// `--host` for the server.
    host: String,
    /// Passed to the server as TOKEN_ENV: the generated desktop token, when
    /// the LAN bind needs one and config.toml sets none.
    sidecar_token: Option<String>,
    /// Injected into the webview so the UI signs itself in: `[web]
    /// auth_token` when set, else the sidecar token.
    webview_token: Option<String>,
}

/// Loopback by default. `[federation] bind_lan = true` exposes the server on
/// the LAN so this machine can act as a federation peer — and a LAN-bound
/// server always has a token: `[web] auth_token` if set, else the generated
/// desktop token (the server only answers loopback without one). If no token
/// can be had, it stays on loopback rather than open the LAN unauthenticated.
/// The webview still connects via 127.0.0.1.
fn plan_launch(home: Option<&Path>) -> LaunchPlan {
    let cfg = home.and_then(read_config);
    let configured = config_str(cfg.as_ref(), "web", "auth_token");
    let loopback = |webview_token: Option<String>| LaunchPlan {
        host: "127.0.0.1".to_string(),
        sidecar_token: None,
        webview_token,
    };
    if !config_bind_lan(cfg.as_ref()) {
        return loopback(configured);
    }
    if configured.is_some() {
        return LaunchPlan {
            host: "0.0.0.0".to_string(),
            sidecar_token: None,
            webview_token: configured,
        };
    }
    match home.map(load_or_create_token) {
        Some(Ok(token)) => LaunchPlan {
            host: "0.0.0.0".to_string(),
            sidecar_token: Some(token.clone()),
            webview_token: Some(token),
        },
        Some(Err(e)) => {
            eprintln!("evi: LAN access needs a token and none could be created ({e}); serving on loopback only");
            loopback(None)
        }
        None => {
            eprintln!("evi: no eVi home for the LAN token; serving on loopback only");
            loopback(None)
        }
    }
}

/// Hand the plan's token (and only it) to a server process. A token in the
/// shell's own environment is dropped so the server's auth matches what the
/// webview is given.
fn apply_launch_env(cmd: &mut Command, plan: &LaunchPlan) {
    match &plan.sidecar_token {
        Some(t) => {
            cmd.env(TOKEN_ENV, t);
        }
        None => {
            cmd.env_remove(TOKEN_ENV);
        }
    }
}

/// The webview initialization script: `window.__EVI_PORT__` for the loading
/// shim, and the access token — set only when the page's origin is one of
/// `origins` (the server's own), so a page the webview navigates to elsewhere
/// never sees it. Values are JSON-encoded, which is a valid JS literal.
fn webview_init_script(port: Option<u16>, token: Option<&str>, origins: &[String]) -> String {
    let mut js = String::new();
    if let Some(p) = port {
        js.push_str(&format!("window.__EVI_PORT__ = {p};"));
    }
    if let Some(t) = token.filter(|t| !t.is_empty()) {
        let origins = serde_json::to_string(origins).unwrap_or_else(|_| "[]".into());
        let token = serde_json::to_string(t).unwrap_or_else(|_| "\"\"".into());
        js.push_str(&format!(
            "(function(){{if({origins}.indexOf(window.location.origin)>=0)\
             {{window.__EVI_TOKEN__={token};}}}})();"
        ));
    }
    js
}

/// The origin (`scheme://host[:port]`) of `url`, for the token guard above.
fn url_origin(url: &str) -> Option<String> {
    let parsed = url::Url::parse(url).ok()?;
    let origin = parsed.origin();
    origin.is_tuple().then(|| origin.ascii_serialization())
}

/// The app commands the eVi web UI calls through Tauri's IPC: app.js's
/// `DESKTOP_COMMANDS`, and the set build.rs declares. The page gets no other
/// Tauri API — `withGlobalTauri` is off, so there is no `window.__TAURI__`.
const UI_COMMANDS: [&str; 9] = [
    "check_for_update_cmd",
    "install_update_cmd",
    "restart_app_cmd",
    "update_available_cmd",
    "dismiss_update_cmd",
    "staged_update_cmd",
    "open_logs_cmd",
    "open_external_cmd",
    "update_status_cmd",
];

/// The permission tauri-build generates for app command `cmd` (build.rs).
fn command_permission(cmd: &str) -> String {
    format!("allow-{}", cmd.replace('_', "-"))
}

/// A URLPattern matching every page of `origin` and nothing else: the IPC
/// capability's remote URL. Characters URLPattern treats as syntax are
/// escaped in the host (an IPv6 literal's `:`, a `*` the URL parser allows in
/// a domain), so the host only matches itself. None for anything but an
/// http(s) origin, or a pattern Tauri can't parse.
fn origin_pattern(origin: &str) -> Option<String> {
    let u = url::Url::parse(origin).ok()?;
    if !matches!(u.scheme(), "http" | "https") {
        return None;
    }
    let mut host = String::new();
    for c in u.host_str()?.chars() {
        if matches!(c, ':' | '*' | '(' | ')' | '{' | '}' | '+' | '?' | '\\') {
            host.push('\\');
        }
        host.push(c);
    }
    let port = u.port().map(|p| format!(":{p}")).unwrap_or_default();
    // No path: Tauri widens an empty path (and search/hash) to `*`.
    let pattern = format!("{}://{host}{port}", u.scheme());
    pattern
        .parse::<tauri::utils::acl::RemoteUrlPattern>()
        .ok()
        .map(|_| pattern)
}

/// The IPC capability for the eVi page: UI_COMMANDS, in the main window, for
/// exactly `origins` — the server's own (the sidecar on 127.0.0.1/localhost at
/// its port, or the EVI_REMOTE_URL server), the same list the access token is
/// bound to. Never the app's bundled pages, and never another localhost port:
/// the static `http://localhost:*` grant this replaces let any local web page
/// the window reached call these commands. None when no origin is usable (the
/// UI then works without the desktop-only actions).
fn ui_capability(origins: &[String]) -> Option<tauri::ipc::CapabilityBuilder> {
    let patterns: Vec<String> = origins.iter().filter_map(|o| origin_pattern(o)).collect();
    if patterns.is_empty() {
        return None;
    }
    let mut cap = tauri::ipc::CapabilityBuilder::new("evi-ui")
        .window("main")
        .local(false);
    for p in patterns {
        cap = cap.remote(p);
    }
    for cmd in UI_COMMANDS {
        cap = cap.permission(command_permission(cmd));
    }
    Some(cap)
}

/// Navigation guard for the main window. It renders model output and holds
/// the access token, so it may only show the eVi server (`origins`, as for
/// the token and the IPC capability) and the app's own bundled pages (the
/// loading shim). A link, a redirect, or a script that got past the sanitizer
/// and the page's CSP can't take it anywhere else; the UI hands http(s) links
/// to the system browser itself (open_external_cmd).
fn navigation_allowed(url: &url::Url, origins: &[String]) -> bool {
    let own = |u: &url::Url| {
        let o = u.origin();
        o.is_tuple() && origins.iter().any(|a| *a == o.ascii_serialization())
    };
    match url.scheme() {
        // The bundled pages: tauri://localhost (macOS/Linux) or
        // http(s)://tauri.localhost (Windows).
        "tauri" => url.host_str() == Some("localhost"),
        "http" | "https" => {
            (url.host_str() == Some("tauri.localhost") && url.port().is_none()) || own(url)
        }
        // A blob: the page made itself (Export Chat), on the page's origin.
        "blob" => own(url),
        _ => false,
    }
}

/// Thin-client mode's token for the `EVI_REMOTE_URL` server: `EVI_REMOTE_TOKEN`
/// (`env_token`), else the token `<home>/peers.json` keeps for a peer at the
/// same origin — the credential this machine already holds for that server.
/// Never this machine's own `[web] auth_token`: that gates a different server,
/// and would be handed to another host.
fn remote_token(env_token: Option<String>, home: Option<&Path>, remote: &str) -> Option<String> {
    if let Some(t) = env_token.map(|t| t.trim().to_string()).filter(|t| !t.is_empty()) {
        return Some(t);
    }
    let origin = url_origin(remote)?;
    let text = std::fs::read_to_string(home?.join("peers.json")).ok()?;
    let peers: serde_json::Value = serde_json::from_str(&text).ok()?;
    peers.as_array()?.iter().find_map(|p| {
        let url = p.get("url")?.as_str()?.trim();
        let token = p.get("token")?.as_str()?.trim();
        (url_origin(url).as_deref() == Some(origin.as_str()) && !token.is_empty())
            .then(|| token.to_string())
    })
}

/// Directory for the spawned server's log, under the eVi home dir. Created
/// on demand; None if it can't be set up.
fn server_log_dir() -> Option<PathBuf> {
    let dir = evi_home()?.join("logs");
    std::fs::create_dir_all(&dir).ok()?;
    Some(dir)
}

/// Configure a child process before spawn: route stdout/stderr to a log
/// file (so a failed server start is debuggable even with no console), and
/// on Windows suppress the console window a console-subsystem child would
/// otherwise pop up. Used for both the sidecar and the dev-python spawn.
fn configure_child(cmd: &mut Command) {
    let mut logged = false;
    if let Some(dir) = server_log_dir() {
        if let Ok(out) = std::fs::File::create(dir.join("desktop-server.log")) {
            if let Ok(err) = out.try_clone() {
                cmd.stdout(Stdio::from(out)).stderr(Stdio::from(err));
                logged = true;
            }
        }
    }
    if !logged {
        cmd.stdout(Stdio::null()).stderr(Stdio::null());
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
}

/// Spawn `python -m uvicorn evi.apps.web.server:app` from the repo root.
/// On Windows we prefer the `py -3.13` launcher; elsewhere fall back to `python3`.
fn spawn_server(repo_root: &Path, port: u16, plan: &LaunchPlan) -> std::io::Result<Child> {
    let (program, prefix_args): (&str, &[&str]) = if cfg!(windows) {
        ("py", &["-3.13", "-m"])
    } else {
        ("python3", &["-m"])
    };

    let host = plan.host.clone();
    let mut cmd = Command::new(program);
    cmd.args(prefix_args)
        .args([
            "uvicorn",
            "evi.apps.web.server:app",
            "--host",
            &host,
            "--port",
            &port.to_string(),
        ])
        .current_dir(repo_root);

    // Honour an override if the user wants a specific interpreter.
    if let Ok(custom) = std::env::var("EVI_PYTHON") {
        cmd = Command::new(custom);
        cmd.args([
            "-m",
            "uvicorn",
            "evi.apps.web.server:app",
            "--host",
            &host,
            "--port",
            &port.to_string(),
        ])
        .current_dir(repo_root);
    }

    apply_launch_env(&mut cmd, plan);
    configure_child(&mut cmd);
    cmd.spawn()
}

/// Locate the frozen `evi-server` sidecar binary, if this is a standalone
/// build. We ship a PyInstaller `--onedir` folder via Tauri
/// `bundle.resources`, so the binary lives under the app's resource dir
/// (`<resources>/evi-server/evi-server[.exe]`). We also check a couple of
/// fallback layouts (different bundlers / a dev-staged folder / the old
/// single-file externalBin layout adjacent to the exe). Returns None when
/// no sidecar is bundled (dev / source checkout) → caller falls back to a
/// system Python.
fn sidecar_path(app: &tauri::App) -> Option<PathBuf> {
    let bin = if cfg!(windows) { "evi-server.exe" } else { "evi-server" };

    // Prefer a sidecar staged by the update channel (a newer core, downloaded +
    // verified on a previous run) over the one bundled in the app — but only when
    // it's actually newer than this bundle, so a full-app update's fresh bundled
    // core is never shadowed by a stale staged one.
    let bundled_version = app.package_info().version.to_string();
    if let Some(p) = evi_home().and_then(|h| sidecar_update::staged_sidecar(&h, &bundled_version)) {
        return Some(p);
    }

    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Ok(res) = app.path().resource_dir() {
        candidates.push(res.join("evi-server").join(bin));
        candidates.push(res.join("binaries").join("evi-server").join(bin));
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            candidates.push(dir.join("evi-server").join(bin)); // onedir adjacent
            candidates.push(dir.join(bin)); // legacy single-file externalBin
        }
    }
    candidates.into_iter().find(|p| p.is_file())
}

/// Spawn the frozen sidecar binary with --host/--port. If a `tesseract`
/// binary was bundled next to it (practical-tier OCR), point the server at
/// it via env so `ocr.py` uses the bundled copy instead of a system one.
fn spawn_sidecar(path: &Path, port: u16, plan: &LaunchPlan) -> std::io::Result<Child> {
    let mut cmd = Command::new(path);
    cmd.args(["--host", &plan.host, "--port", &port.to_string()]);
    apply_launch_env(&mut cmd, plan);

    if let Some(dir) = path.parent() {
        let tess = dir.join(if cfg!(windows) { "tesseract.exe" } else { "tesseract" });
        if tess.is_file() {
            cmd.env("EVI_TESSERACT_CMD", &tess);
            let tessdata = dir.join("tessdata");
            if tessdata.is_dir() {
                cmd.env("TESSDATA_PREFIX", &tessdata);
            }
        }
    }
    configure_child(&mut cmd);
    cmd.spawn()
}

/// Poll `<base>/api/health` until it succeeds or we time out. Used in remote
/// mode, where the base URL already includes host:port (and optional path).
fn wait_for_health_url(base: &str, timeout: Duration) -> bool {
    let trimmed = base.trim_end_matches('/');
    let url = format!("{}/api/health", trimmed);
    let deadline = Instant::now() + timeout;
    // ureq 3 moved per-request timeouts onto the agent's config.
    let agent: ureq::Agent = ureq::Agent::config_builder()
        .timeout_global(Some(Duration::from_millis(500)))
        .build()
        .into();
    while Instant::now() < deadline {
        if let Ok(resp) = agent.get(&url).call() {
            let code = resp.status().as_u16();
            if (200..300).contains(&code) {
                return true;
            }
        }
        thread::sleep(Duration::from_millis(250));
    }
    false
}

/// Background self-update: ask our GitHub releases (the signed bundles +
/// `latest.json` that `desktop-release.yml` publishes) whether a newer version
/// exists; if so, download, install, and restart. Runs on the async runtime so
/// it never blocks the window. Opt out with `EVI_AUTO_UPDATE=0`. The updater
/// only accepts bundles signed with our key (pubkey in tauri.conf.json), so a
/// tampered release can't be installed.
fn spawn_update_check(handle: tauri::AppHandle) {
    use tauri_plugin_updater::UpdaterExt;

    if std::env::var("EVI_AUTO_UPDATE")
        .map(|v| v == "0" || v.eq_ignore_ascii_case("false"))
        .unwrap_or(false)
    {
        return;
    }

    tauri::async_runtime::spawn(async move {
        let updater = match handle.updater() {
            Ok(u) => u,
            Err(e) => {
                eprintln!("evi: updater unavailable: {e}");
                return;
            }
        };
        match updater.check().await {
            Ok(Some(update)) => {
                // Don't auto-install. Record the offer; the webview polls
                // `update_available_cmd`, prompts (OK/Cancel), and only installs
                // on the user's explicit OK via `install_update_cmd`.
                eprintln!("evi: update {} available — prompting", update.version);
                let current = handle.package_info().version.to_string();
                if let Some(st) = handle.try_state::<AvailableState>() {
                    *st.0.lock().unwrap() = Some(AvailableUpdate {
                        version: update.version.clone(),
                        current,
                    });
                }
            }
            Ok(None) => eprintln!("evi: already up to date"),
            Err(e) => eprintln!("evi: update check failed: {e}"),
        }
    });
}

/// Download + install an update and relaunch. Shared by the launch-time
/// auto-check and Help → Check for Updates.
///
/// The sidecar MUST die before the install step: the NSIS updater overwrites its
/// onedir files (e.g. _internal/VCRUNTIME140.dll), which Windows keeps locked
/// while evi-server runs — otherwise the installer fails with "Error opening file
/// for writing". But we kill it in the *download-finished* callback (right before
/// install), NOT before the download — so a failed download (the common failure:
/// network drop) leaves the backend fully alive. If the install itself then fails
/// after the kill, we relaunch to recover the now-dead backend rather than leave
/// the window pointed at a dead server.
async fn install_and_restart(handle: tauri::AppHandle, update: tauri_plugin_updater::Update) {
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;

    let version = update.version.clone();
    set_update(&handle, "downloading", &version);
    if let Some(w) = handle.get_webview_window("main") {
        let _ = w.eval(
            "window.eviUI && window.eviUI.onUpdateStarted && window.eviUI.onUpdateStarted()",
        );
    }
    let h = handle.clone();
    let on_chunk = move |chunk: usize, total: Option<u64>| {
        if let Some(st) = h.try_state::<UpdateState>() {
            let mut p = st.0.lock().unwrap();
            p.downloaded += chunk as u64;
            if let Some(t) = total {
                p.total = t;
            }
        }
    };
    // Kill the sidecar only once the download has finished, immediately before
    // the install overwrites its (locked) files. `killed` records that so the
    // error branch knows whether the backend is dead and needs a relaunch.
    let killed = Arc::new(AtomicBool::new(false));
    let kill_handle = handle.clone();
    let killed_flag = killed.clone();
    let on_download_finished = move || {
        if let Some(mut child) = kill_handle.state::<ServerHandle>().0.lock().unwrap().take() {
            let _ = child.kill();
            let _ = child.wait();
        }
        killed_flag.store(true, Ordering::SeqCst);
    };
    match update.download_and_install(on_chunk, on_download_finished).await {
        Ok(_) => {
            set_update(&handle, "installing", &version);
            eprintln!("evi: update installed — restarting");
            handle.restart();
        }
        Err(e) => {
            set_update(&handle, "error", &version);
            eprintln!("evi: update install failed: {e}");
            // If we already killed the sidecar (failure happened during install,
            // not download), the backend is dead — relaunch to bring it back.
            if killed.load(Ordering::SeqCst) {
                handle.restart();
            }
        }
    }
}

/// Live progress for the in-app update toast (polled via `update_status_cmd`).
#[derive(Default, Clone, serde::Serialize)]
struct UpdateProgress {
    phase: String, // "" | downloading | installing | error
    downloaded: u64,
    total: u64,
    version: String,
}

struct UpdateState(std::sync::Mutex<UpdateProgress>);

/// A full-app update the launch-time check found but has NOT installed. The
/// webview polls `update_available_cmd`, shows an OK/Cancel prompt, and only on
/// the user's OK does `install_update_cmd` proceed — no more silent auto-install.
#[derive(Default, Clone, serde::Serialize)]
struct AvailableUpdate {
    version: String,
    current: String,
}
struct AvailableState(std::sync::Mutex<Option<AvailableUpdate>>);

/// A newer *core* (sidecar) that the sidecar update channel has staged, pending
/// a restart to take effect. The webview polls `staged_update_cmd` and offers a
/// one-click "Restart now", so the running version actually advances instead of
/// silently lagging until the user happens to fully quit (closing the window
/// only hides to tray, keeping the old sidecar warm).
struct StagedState(std::sync::Mutex<Option<String>>);

fn set_update(handle: &tauri::AppHandle, phase: &str, version: &str) {
    if let Some(st) = handle.try_state::<UpdateState>() {
        let mut p = st.0.lock().unwrap();
        p.phase = phase.to_string();
        if !version.is_empty() {
            p.version = version.to_string();
        }
        if phase == "downloading" {
            p.downloaded = 0;
            p.total = 0;
        }
    }
}

/// Current update progress for the webview's poll loop.
#[tauri::command]
fn update_status_cmd(app: tauri::AppHandle) -> UpdateProgress {
    app.state::<UpdateState>().0.lock().unwrap().clone()
}

/// Verdict returned to the webview's Help → Check for Updates dialog.
#[derive(serde::Serialize)]
struct UpdateStatus {
    available: bool,
    version: String,
    current: String,
}

/// Help → Check for Updates. Returns the verdict ONLY — nothing installs here.
/// The webview shows an OK/Cancel prompt when `available`, and installs on OK via
/// `install_update_cmd`. This keeps the "ask before upgrading" contract for the
/// explicit-check path too.
#[tauri::command]
async fn check_for_update_cmd(app: tauri::AppHandle) -> Result<UpdateStatus, String> {
    use tauri_plugin_updater::UpdaterExt;
    let current = app.package_info().version.to_string();
    let updater = app.updater().map_err(|e| e.to_string())?;
    match updater.check().await {
        Ok(Some(update)) => Ok(UpdateStatus {
            available: true,
            version: update.version.clone(),
            current,
        }),
        Ok(None) => Ok(UpdateStatus { available: false, version: String::new(), current }),
        Err(e) => Err(e.to_string()),
    }
}

/// The pending full-app update offer (set by the launch-time check), or None.
/// Polled by the webview to raise the "Update available" prompt.
#[tauri::command]
fn update_available_cmd(app: tauri::AppHandle) -> Option<AvailableUpdate> {
    app.state::<AvailableState>().0.lock().unwrap().clone()
}

/// User chose "Not now" — clear the offer so the prompt doesn't re-fire this
/// session (a fresh check on the next launch will re-offer).
#[tauri::command]
fn dismiss_update_cmd(app: tauri::AppHandle) {
    *app.state::<AvailableState>().0.lock().unwrap() = None;
}

/// The staged newer core awaiting a restart (set by the sidecar channel), or
/// None. Polled by the webview to raise the "Restart to apply" prompt.
#[tauri::command]
fn staged_update_cmd(app: tauri::AppHandle) -> Option<String> {
    app.state::<StagedState>().0.lock().unwrap().clone()
}

/// User approved the full-app update (OK on the prompt). Re-check and, if still
/// available, download + install + restart (with the progress toast). Returns
/// `true` when an install is under way; `false` when the offer is gone (the
/// release was yanked/replaced between the offer and the OK) so the webview can
/// clear its "Downloading…" message instead of hanging on it forever.
#[tauri::command]
async fn install_update_cmd(app: tauri::AppHandle) -> Result<bool, String> {
    use tauri_plugin_updater::UpdaterExt;
    *app.state::<AvailableState>().0.lock().unwrap() = None; // consume the offer
    let updater = app.updater().map_err(|e| e.to_string())?;
    match updater.check().await {
        Ok(Some(update)) => {
            install_and_restart(app.clone(), update).await;
            Ok(true)
        }
        Ok(None) => Ok(false), // no longer available
        Err(e) => Err(e.to_string()),
    }
}

/// Full, clean restart used by the "Restart now" prompt after the sidecar
/// channel stages a newer core. Kills the sidecar first — so the newly-staged
/// core can boot and its (Windows-locked) files unlock — then relaunches, at
/// which point `sidecar_path` picks up the new staged version.
#[tauri::command]
fn restart_app_cmd(app: tauri::AppHandle) {
    if let Some(mut child) = app.state::<ServerHandle>().0.lock().unwrap().take() {
        let _ = child.kill();
        let _ = child.wait();
    }
    app.restart();
}

/// Open the eVi logs folder in the OS file manager (Help → Open Logs Folder).
#[tauri::command]
fn open_logs_cmd() -> Result<(), String> {
    match server_log_dir() {
        Some(dir) => os_open_dir(&dir).map_err(|e| e.to_string()),
        None => Err("no log directory".into()),
    }
}

/// Open an external http(s) URL in the default browser. Invokable from the
/// webview — which renders model output — so the URL is strictly validated and
/// only its normalized form is handed to the OS, never through a shell.
#[tauri::command]
fn open_external_cmd(url: String) -> Result<(), String> {
    let url = validate_external_url(&url)?;
    os_open_url(&url).map_err(|e| e.to_string())
}

/// Strictly validate a URL before it leaves the app: parse it with the `url`
/// crate, allow only http/https with a real host (an IP literal or a DNS-style
/// name) and no embedded credentials, and refuse whitespace and control
/// characters in the raw input (the parser would silently strip tabs/newlines).
/// Quotes are percent-encoded rather than refused, and the normalized form is
/// re-checked (host percent-decoding can introduce e.g. `"`). Returns the
/// normalized serialization — the only string the OS ever sees.
fn validate_external_url(raw: &str) -> Result<String, String> {
    fn bad_char(c: char) -> bool {
        c.is_control() || c.is_whitespace()
    }
    fn quote(c: char) -> bool {
        matches!(c, '"' | '\'' | '`')
    }
    if raw.is_empty() {
        return Err("empty URL".into());
    }
    if raw.chars().any(bad_char) {
        return Err("URL contains whitespace or control characters".into());
    }
    let parsed = url::Url::parse(raw).map_err(|e| format!("invalid URL: {e}"))?;
    if !matches!(parsed.scheme(), "http" | "https") {
        return Err("only http(s) URLs are allowed".into());
    }
    // `https://github.com@evil.com/` opens evil.com — a phishing disguise, and
    // nothing eVi links to needs credentials in the URL.
    if !parsed.username().is_empty() || parsed.password().is_some() {
        return Err("URLs with embedded credentials are not allowed".into());
    }
    // WHATWG host rules (which `url` follows) only forbid a few code points, so
    // a host like `x&calc.exe` parses. No DNS name looks like that — refuse
    // anything but [A-Za-z0-9._-] (IDN hosts are punycode by now) as defense in
    // depth on top of never using a shell.
    let host_ok = match parsed.host() {
        Some(url::Host::Domain(d)) => {
            let dns_char = |c: char| c.is_ascii_alphanumeric() || matches!(c, '-' | '.' | '_');
            !d.is_empty() && d.chars().all(dns_char)
        }
        Some(url::Host::Ipv4(_)) | Some(url::Host::Ipv6(_)) => true,
        _ => false,
    };
    if !host_ok {
        return Err("URL has no valid host".into());
    }
    // The parser percent-encodes `"` everywhere but leaves `'` raw in paths and
    // fragments and `` ` `` raw in queries. Encode those too (WHATWG already
    // encodes `'` in http(s) queries; servers decode both back) so no quote ever
    // reaches the OS — old xdg-open versions mis-quote them — without refusing
    // links like `.../wiki/Moore's_law`. The host allowlist above keeps quotes
    // out of the host, so this only touches path/query/fragment.
    let normalized = parsed.as_str().replace('\'', "%27").replace('`', "%60");
    if !normalized.is_ascii() || normalized.chars().any(|c| bad_char(c) || quote(c)) {
        return Err("URL contains whitespace, quotes or control characters".into());
    }
    Ok(normalized)
}

/// Open an already-validated http(s) URL in the default browser, always as a
/// single argument and never through a shell.
///
/// Windows: NOT `cmd /C start "" <url>` — cmd.exe re-parses the whole command
/// line (`&`, `|`, `^`, `%VAR%`), so `http://x&calc.exe` ran calc and real URLs
/// with `&` broke. `rundll32 url.dll,FileProtocolHandler <url>` instead: rundll32
/// passes everything after the entry point verbatim to FileProtocolHandler,
/// which ShellExecutes it — the same default-browser lookup `start` did, minus
/// the shell. `explorer.exe <url>` was rejected because explorer treats commas
/// as switch separators, and the shell plugin's opener isn't initialized in
/// this app (no `.plugin(tauri_plugin_shell::init())`). The validator guarantees no
/// whitespace or quotes, so std's argument quoting leaves the URL untouched.
/// rundll32 is a GUI-subsystem binary, so no CREATE_NO_WINDOW is needed. It is
/// run by absolute path under %SystemRoot% (`windows_system_exe`).
fn os_open_url(url: &str) -> std::io::Result<()> {
    url_open_command(Opener::host(), url).spawn().map(|_| ())
}

/// Which OS opener to build a command for. A runtime value rather than
/// `#[cfg]` blocks so every OS's argv — the Windows no-shell one included — is
/// compiled and unit-tested on any CI host (CI's `rust` job is Linux-only).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Opener {
    Windows,
    MacOs,
    Xdg,
}

impl Opener {
    fn host() -> Opener {
        if cfg!(windows) {
            Opener::Windows
        } else if cfg!(target_os = "macos") {
            Opener::MacOs
        } else {
            Opener::Xdg
        }
    }
}

/// The Windows directory (`%SystemRoot%`, else `%windir%`, else `C:\Windows`),
/// without a trailing separator. Only an absolute drive path is taken, so the
/// openers below never resolve through the search path.
fn windows_root() -> String {
    windows_root_from(
        std::env::var("SystemRoot")
            .ok()
            .or_else(|| std::env::var("windir").ok()),
    )
}

fn windows_root_from(value: Option<String>) -> String {
    value
        .map(|v| v.trim().trim_end_matches(['\\', '/']).to_string())
        .filter(|v| {
            let b = v.as_bytes();
            b.len() >= 3 && b[0].is_ascii_alphabetic() && b[1] == b':' && matches!(b[2], b'\\' | b'/')
        })
        .unwrap_or_else(|| r"C:\Windows".to_string())
}

/// A system binary by absolute path (`<root>\System32\rundll32.exe`, …).
/// A bare name would let Windows search the app's own folder first, so a DLL-
/// or EXE-planted `rundll32.exe` next to evi-desktop.exe would run instead.
/// Built as a string, not a PathBuf, so the Windows argv is identical (and
/// tested) on every build host.
fn windows_system_exe(rel: &str) -> String {
    format!(r"{}\{}", windows_root(), rel)
}

/// The (unspawned) opener command for `os_open_url` — split out so tests can
/// assert the program and argv without launching a browser.
fn url_open_command(os: Opener, url: &str) -> Command {
    let mut cmd = match os {
        Opener::Windows => {
            let mut c = Command::new(windows_system_exe(r"System32\rundll32.exe"));
            c.arg("url.dll,FileProtocolHandler");
            c
        }
        Opener::MacOs => Command::new("open"),
        Opener::Xdg => Command::new("xdg-open"),
    };
    cmd.arg(url);
    cmd
}

/// Open a local directory in the OS file manager, passed as a single argument.
/// Windows uses `%SystemRoot%\explorer.exe <dir>` (browse only, no cmd.exe re-parsing; a
/// GUI-subsystem binary, so no CREATE_NO_WINDOW). Never used for URLs.
fn os_open_dir(dir: &Path) -> std::io::Result<()> {
    dir_open_command(Opener::host(), dir).spawn().map(|_| ())
}

/// The (unspawned) opener command for `os_open_dir`, split out for tests.
fn dir_open_command(os: Opener, dir: &Path) -> Command {
    let mut cmd = Command::new(match os {
        Opener::Windows => windows_system_exe("explorer.exe"),
        Opener::MacOs => "open".to_string(),
        Opener::Xdg => "xdg-open".to_string(),
    });
    cmd.arg(dir);
    cmd
}

/// Build the File/Edit/View/Help menu bar. Custom items carry stable ids that
/// `dispatch_menu_action` routes; predefined Edit items are handled natively by
/// the OS against the focused field.
fn build_menu(app: &tauri::App) -> tauri::Result<tauri::menu::Menu<tauri::Wry>> {
    let new_chat = MenuItemBuilder::with_id("new_chat", "New Chat")
        .accelerator("CmdOrCtrl+N")
        .build(app)?;
    let open_file = MenuItemBuilder::with_id("open_file", "Open File…")
        .accelerator("CmdOrCtrl+O")
        .build(app)?;
    let export_chat = MenuItemBuilder::with_id("export_chat", "Export Chat…")
        .accelerator("CmdOrCtrl+S")
        .build(app)?;
    let rewind = MenuItemBuilder::with_id("rewind", "Undo File Change…").build(app)?;
    let settings = MenuItemBuilder::with_id("settings", "Settings…").build(app)?;
    let quit = MenuItemBuilder::with_id("quit", "Exit")
        .accelerator("CmdOrCtrl+Q")
        .build(app)?;
    let file = SubmenuBuilder::new(app, "File")
        .item(&new_chat)
        .item(&open_file)
        .item(&export_chat)
        .item(&rewind)
        .separator()
        .item(&settings)
        .separator()
        .item(&PredefinedMenuItem::close_window(app, Some("Close Window"))?)
        .item(&quit)
        .build()?;

    let find = MenuItemBuilder::with_id("find", "Find…")
        .accelerator("CmdOrCtrl+F")
        .build(app)?;
    let edit = SubmenuBuilder::new(app, "Edit")
        .item(&PredefinedMenuItem::undo(app, Some("Undo"))?)
        .item(&PredefinedMenuItem::redo(app, Some("Redo"))?)
        .separator()
        .item(&PredefinedMenuItem::cut(app, Some("Cut"))?)
        .item(&PredefinedMenuItem::copy(app, Some("Copy"))?)
        .item(&PredefinedMenuItem::paste(app, Some("Paste"))?)
        .item(&PredefinedMenuItem::select_all(app, Some("Select All"))?)
        .separator()
        .item(&find)
        .build()?;

    let reload = MenuItemBuilder::with_id("reload", "Reload")
        .accelerator("CmdOrCtrl+R")
        .build(app)?;
    let zoom_in = MenuItemBuilder::with_id("zoom_in", "Zoom In").build(app)?;
    let zoom_out = MenuItemBuilder::with_id("zoom_out", "Zoom Out").build(app)?;
    let zoom_reset = MenuItemBuilder::with_id("zoom_reset", "Reset Zoom").build(app)?;
    let toggle_theme = MenuItemBuilder::with_id("toggle_theme", "Toggle Theme").build(app)?;
    let devtools = MenuItemBuilder::with_id("toggle_devtools", "Toggle Developer Tools (Console)")
        .accelerator("F12")
        .build(app)?;
    let view = SubmenuBuilder::new(app, "View")
        .item(&reload)
        .separator()
        .item(&zoom_in)
        .item(&zoom_out)
        .item(&zoom_reset)
        .separator()
        .item(&toggle_theme)
        .item(&devtools)
        .build()?;

    let docs = MenuItemBuilder::with_id("documentation", "Documentation").build(app)?;
    let shortcuts = MenuItemBuilder::with_id("shortcuts", "Keyboard Shortcuts").build(app)?;
    let updates = MenuItemBuilder::with_id("check_updates", "Check for Updates…").build(app)?;
    let diagnostics = MenuItemBuilder::with_id("diagnostics", "Run Diagnostics…").build(app)?;
    let logs = MenuItemBuilder::with_id("open_logs", "Open Logs Folder").build(app)?;
    let support = MenuItemBuilder::with_id("get_support", "Get Support").build(app)?;
    let about = MenuItemBuilder::with_id("about", "About eVi").build(app)?;
    let help = SubmenuBuilder::new(app, "Help")
        .item(&docs)
        .item(&shortcuts)
        .separator()
        .item(&updates)
        .item(&diagnostics)
        .item(&logs)
        .separator()
        .item(&support)
        .item(&about)
        .build()?;

    MenuBuilder::new(app)
        .item(&file)
        .item(&edit)
        .item(&view)
        .item(&help)
        .build()
}

/// Run a webview-side menu action by calling the JS bridge directly. We use
/// `eval` rather than `emit` because the page has no Tauri event API
/// (`withGlobalTauri` is off, and its capability grants only UI_COMMANDS) —
/// so an emitted event would have no listener. eval always reaches the page.
fn run_webview_action(app: &tauri::AppHandle, id: &str) {
    if let Some(w) = app.get_webview_window("main") {
        // ids are fixed [a-z_] literals, so no escaping is needed.
        let _ = w.eval(&format!("window.eviUI && window.eviUI.handleMenu('{id}')"));
    }
}

/// Route a menu/tray action. Devtools + quit are handled natively here; every
/// other id is run in the webview via the JS bridge (window.eviUI.handleMenu).
fn dispatch_menu_action(app: &tauri::AppHandle, id: &str) {
    match id {
        "toggle_devtools" => {
            if let Some(w) = app.get_webview_window("main") {
                if w.is_devtools_open() {
                    w.close_devtools();
                } else {
                    w.open_devtools();
                }
            }
        }
        "quit" => app.exit(0),
        other => run_webview_action(app, other),
    }
}

/// Show + focus the main window (used by the tray).
fn show_main_window(app: &tauri::AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

/// Build the system tray. Left-click shows the window; the menu mirrors the
/// most-used actions. Closing the window hides it here rather than quitting.
fn build_tray(app: &tauri::App) -> tauri::Result<()> {
    let show = MenuItemBuilder::with_id("tray_show", "Show eVi").build(app)?;
    let new_chat = MenuItemBuilder::with_id("tray_new_chat", "New Chat").build(app)?;
    let updates = MenuItemBuilder::with_id("tray_updates", "Check for Updates…").build(app)?;
    let quit = MenuItemBuilder::with_id("tray_quit", "Quit eVi").build(app)?;
    let menu = MenuBuilder::new(app)
        .item(&show)
        .item(&new_chat)
        .item(&updates)
        .separator()
        .item(&quit)
        .build()?;

    let mut builder = TrayIconBuilder::with_id("main")
        .tooltip("eVi")
        .menu(&menu)
        .on_menu_event(|app, event| {
            let id = event.id().as_ref();
            if id == "tray_quit" {
                app.exit(0);
                return;
            }
            show_main_window(app);
            match id {
                "tray_new_chat" => run_webview_action(app, "new_chat"),
                "tray_updates" => run_webview_action(app, "check_updates"),
                _ => {}
            }
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_main_window(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }
    builder.build(app)?;
    Ok(())
}

/// Map an `evi://` deep link to the in-app web path to load. Mirrors
/// `evi.deeplinks.to_web_path`. Unknown routes fall back to `/`.
fn deep_link_to_path(raw: &str) -> Option<String> {
    let u = url::Url::parse(raw).ok()?;
    if u.scheme() != "evi" {
        return None;
    }
    let kind = u.host_str().unwrap_or("");
    let value = u.path().trim_start_matches('/').to_string();
    Some(match kind {
        "session" if !value.is_empty() => format!("/?session={}", value),
        "workflow" if !value.is_empty() => format!("/?workflow={}", value),
        _ => "/".to_string(),
    })
}

fn main() {
    let server = ServerHandle(Mutex::new(None));

    let app = tauri::Builder::default()
        .manage(server)
        .manage(UpdateState(std::sync::Mutex::new(UpdateProgress::default())))
        .manage(AvailableState(std::sync::Mutex::new(None)))
        .manage(StagedState(std::sync::Mutex::new(None)))
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_deep_link::init())
        .invoke_handler(tauri::generate_handler![
            check_for_update_cmd,
            install_update_cmd,
            restart_app_cmd,
            update_available_cmd,
            dismiss_update_cmd,
            staged_update_cmd,
            open_logs_cmd,
            open_external_cmd,
            update_status_cmd
        ])
        .on_menu_event(|app, event| dispatch_menu_action(app, event.id().as_ref()))
        .setup(|app| {
            // `local_port` is Some in local mode; the loading shim polls that
            // port and redirects once the server is up.
            let mut local_port: Option<u16> = None;
            // Origin to navigate to when an evi:// deep link arrives. Both the
            // remote and local branches below set it before it's read.
            #[allow(unused_assignments)]
            let mut nav_base: Option<String> = None;
            // The access token the webview signs in with, and the origins
            // (the server's own) whose pages may see it.
            let webview_token: Option<String>;
            let token_origins: Vec<String>;

            // Remote mode: skip the Python spawn and just navigate to the URL.
            // Useful when this laptop is a thin client pointed at the AI server.
            let target = if let Ok(remote) = std::env::var("EVI_REMOTE_URL") {
                let trimmed = remote.trim();
                if trimmed.is_empty() {
                    return Err("EVI_REMOTE_URL is set but empty".into());
                }
                let healthy = wait_for_health_url(trimmed, Duration::from_secs(15));
                if !healthy {
                    eprintln!(
                        "warning: remote {} did not respond to /api/health in time; \
                         loading anyway",
                        trimmed
                    );
                }
                nav_base = Some(trimmed.trim_end_matches('/').to_string());
                // The remote server's token: EVI_REMOTE_TOKEN, else the one
                // peers.json keeps for that server. Without one, the UI's
                // login prompt still works.
                webview_token = remote_token(
                    std::env::var("EVI_REMOTE_TOKEN").ok(),
                    evi_home().as_deref(),
                    trimmed,
                );
                token_origins = url_origin(trimmed).into_iter().collect();
                WebviewUrl::External(trimmed.parse().map_err(|e: url::ParseError| e.to_string())?)
            } else {
                // Local mode. Prefer a bundled sidecar (standalone build);
                // otherwise fall back to spawning a system Python from the
                // repo root (developer / source-checkout install).
                let port = pick_port()?;
                let plan = plan_launch(evi_home().as_deref());
                let child = if let Some(side) = sidecar_path(app) {
                    spawn_sidecar(&side, port, &plan)?
                } else {
                    let exe = std::env::current_exe()?;
                    let start = exe.parent().unwrap_or(Path::new("."));
                    let repo_root = std::env::var("EVI_REPO_ROOT")
                        .ok()
                        .map(PathBuf::from)
                        .or_else(|| find_repo_root(start))
                        .ok_or("could not locate evi repo root (set EVI_REPO_ROOT) \
                                and no bundled sidecar found")?;
                    spawn_server(&repo_root, port, &plan)?
                };
                app.state::<ServerHandle>().0.lock().unwrap().replace(child);
                local_port = Some(port);
                nav_base = Some(format!("http://127.0.0.1:{}", port));
                webview_token = plan.webview_token;
                token_origins = vec![
                    format!("http://127.0.0.1:{port}"),
                    format!("http://localhost:{port}"),
                ];

                // Show the loading shim immediately — do NOT block here waiting
                // for health. The onefile sidecar's cold start (unpacking +
                // importing) is slow and variable; the shim polls the port and
                // redirects when ready, so the user never sees a refused page.
                WebviewUrl::App("index.html".into())
            };

            // IPC for the eVi page: UI_COMMANDS on the server's own origin only.
            match ui_capability(&token_origins) {
                Some(cap) => app.add_capability(cap)?,
                None => eprintln!("evi: no usable server origin; desktop commands disabled"),
            }
            // Where the window may go: the server, plus (dev builds) the dev
            // server that serves the loading shim in place of the bundle.
            let mut nav_origins = token_origins.clone();
            if tauri::is_dev() {
                if let Some(dev) = &app.config().build.dev_url {
                    nav_origins.push(dev.origin().ascii_serialization());
                }
            }

            let mut builder = WebviewWindowBuilder::new(app, "main", target)
                .title("eVi")
                .inner_size(1100.0, 800.0)
                .min_inner_size(600.0, 400.0)
                .resizable(true)
                .on_navigation(move |url| {
                    let ok = navigation_allowed(url, &nav_origins);
                    if !ok {
                        eprintln!(
                            "evi: refused to navigate the window to {}",
                            url.origin().ascii_serialization()
                        );
                    }
                    ok
                });
            let init = webview_init_script(local_port, webview_token.as_deref(), &token_origins);
            if !init.is_empty() {
                builder = builder.initialization_script(init);
            }
            let window = builder.build()?;
            window.show()?;
            window.set_focus()?;

            // Native menu bar + system tray (eVi 0.2.5). Closing the window
            // hides it to the tray instead of quitting, so the assistant (and
            // its warm sidecar) keeps running — quit via tray or File → Exit.
            let menu = build_menu(app)?;
            app.set_menu(menu)?;
            build_tray(app)?;
            let win = window.clone();
            window.on_window_event(move |event| {
                if let WindowEvent::CloseRequested { api, .. } = event {
                    api.prevent_close();
                    let _ = win.hide();
                }
            });

            // evi:// deep links — focus the app on the linked session/workflow.
            // The installer registers the scheme via tauri.conf.json; we also
            // register at runtime (best-effort) for dev / portable runs.
            {
                use tauri_plugin_deep_link::DeepLinkExt;
                let _ = app.deep_link().register_all();
                let dl_win = window.clone();
                let dl_base = nav_base.clone();
                app.deep_link().on_open_url(move |event| {
                    for url in event.urls() {
                        if let Some(path) = deep_link_to_path(url.as_str()) {
                            let full = match &dl_base {
                                Some(b) => format!("{}{}", b, path),
                                None => path,
                            };
                            let _ = dl_win.eval(&format!("window.location.href = {:?};", full));
                            let _ = dl_win.show();
                            let _ = dl_win.set_focus();
                        }
                    }
                });
            }

            // Background self-update against our signed GitHub releases (built
            // by desktop-release.yml). Never blocks launch. Skipped in remote
            // mode — there's no local app bundle to replace — and opt-out via
            // EVI_AUTO_UPDATE=0.
            if std::env::var("EVI_REMOTE_URL").is_err() {
                spawn_update_check(app.handle().clone());
                // Sidecar update channel: background-check for a newer core and
                // stage it for next launch (opt out with EVI_SIDECAR_UPDATE=0).
                // When one is staged, record it so the webview can offer a
                // one-click "Restart now" instead of the user having to fully quit.
                if let Some(home) = evi_home() {
                    let staged_handle = app.handle().clone();
                    sidecar_update::spawn_check(
                        home,
                        app.package_info().version.to_string(),
                        move |v| {
                            if let Some(st) = staged_handle.try_state::<StagedState>() {
                                *st.0.lock().unwrap() = Some(v);
                            }
                        },
                    );
                }
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error building tauri application");

    app.run(|app_handle, event| {
        if let RunEvent::ExitRequested { .. } | RunEvent::Exit = event {
            if let Some(mut child) = app_handle
                .state::<ServerHandle>()
                .0
                .lock()
                .unwrap()
                .take()
            {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    });
}

#[cfg(test)]
mod deep_link_tests {
    // `deep_link_to_path` is private, but a child module can reach its
    // ancestor's private items. It mirrors `evi.deeplinks.to_web_path`
    // (tests/test_deeplinks.py), so these guard the shell/Python parity that a
    // desktop `evi://` deep link depends on.
    use super::deep_link_to_path;

    #[test]
    fn session_link_maps_to_query() {
        assert_eq!(
            deep_link_to_path("evi://session/abc123").as_deref(),
            Some("/?session=abc123")
        );
    }

    #[test]
    fn workflow_link_maps_to_query() {
        assert_eq!(
            deep_link_to_path("evi://workflow/nightly").as_deref(),
            Some("/?workflow=nightly")
        );
    }

    #[test]
    fn empty_value_falls_back_to_root() {
        // `evi://session/` with no id must NOT emit a dangling "/?session=".
        assert_eq!(deep_link_to_path("evi://session/").as_deref(), Some("/"));
        assert_eq!(deep_link_to_path("evi://session").as_deref(), Some("/"));
        assert_eq!(deep_link_to_path("evi://workflow/").as_deref(), Some("/"));
    }

    #[test]
    fn new_and_unknown_routes_fall_back_to_root() {
        // `evi://new` (parity with the Python "new" arm) and any unknown host.
        assert_eq!(deep_link_to_path("evi://new").as_deref(), Some("/"));
        assert_eq!(deep_link_to_path("evi://bogus/x").as_deref(), Some("/"));
    }

    #[test]
    fn non_evi_scheme_and_garbage_are_rejected() {
        assert_eq!(deep_link_to_path("http://example.com/session/x"), None);
        assert_eq!(deep_link_to_path("https://evi/session/x"), None);
        assert_eq!(deep_link_to_path("not a url"), None);
        assert_eq!(deep_link_to_path(""), None);
    }
}

#[cfg(test)]
mod launch_token_tests {
    // The LAN token handshake (SHELL_ABI 2): a LAN-bound sidecar always gets a
    // token, the webview gets the same one, and it survives restarts.
    use super::{
        apply_launch_env, plan_launch, remote_token, url_origin, valid_token,
        webview_init_script, DESKTOP_TOKEN_FILE, TOKEN_ENV,
    };
    use std::path::PathBuf;
    use std::process::Command;

    fn home(tag: &str, config: Option<&str>) -> PathBuf {
        let d = std::env::temp_dir().join(format!("evi-tok-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        if let Some(c) = config {
            std::fs::write(d.join("config.toml"), c).unwrap();
        }
        d
    }

    #[test]
    fn loopback_by_default_with_no_token() {
        let h = home("default", None);
        let plan = plan_launch(Some(&h));
        assert_eq!(plan.host, "127.0.0.1");
        assert_eq!(plan.sidecar_token, None);
        assert_eq!(plan.webview_token, None);
        assert!(!h.join(DESKTOP_TOKEN_FILE).exists(), "no token minted for loopback");
        let _ = std::fs::remove_dir_all(&h);
    }

    #[test]
    fn bind_lan_generates_persists_and_reuses_a_token() {
        let h = home("lan", Some("[federation]\nbind_lan = true\n"));
        let first = plan_launch(Some(&h));
        assert_eq!(first.host, "0.0.0.0");
        let tok = first.sidecar_token.clone().expect("LAN bind without a token");
        assert!(valid_token(&tok), "{tok}");
        assert_eq!(first.webview_token.as_deref(), Some(tok.as_str()));
        // Persisted, user-only, and the same on the next launch.
        let stored = std::fs::read_to_string(h.join(DESKTOP_TOKEN_FILE)).unwrap();
        assert_eq!(stored.trim(), tok);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(h.join(DESKTOP_TOKEN_FILE)).unwrap().permissions().mode();
            assert_eq!(mode & 0o777, 0o600);
        }
        assert_eq!(plan_launch(Some(&h)), first);
        let _ = std::fs::remove_dir_all(&h);
    }

    #[test]
    fn tampered_token_file_is_replaced() {
        let h = home("bad", Some("[federation]\nbind_lan = true\n"));
        std::fs::write(h.join(DESKTOP_TOKEN_FILE), "short\n").unwrap();
        let tok = plan_launch(Some(&h)).sidecar_token.unwrap();
        assert!(valid_token(&tok) && tok != "short");
        let _ = std::fs::remove_dir_all(&h);
    }

    #[test]
    fn configured_token_wins_and_is_not_duplicated_in_env() {
        let h = home(
            "cfg",
            Some("[web]\nauth_token = \"from-config\"\n[federation]\nbind_lan = true\n"),
        );
        let plan = plan_launch(Some(&h));
        assert_eq!(plan.host, "0.0.0.0");
        // The server reads config.toml itself, so a rotation there takes
        // effect at once; the webview signs in with that token.
        assert_eq!(plan.sidecar_token, None);
        assert_eq!(plan.webview_token.as_deref(), Some("from-config"));
        assert!(!h.join(DESKTOP_TOKEN_FILE).exists());
        // Loopback with a configured token: the UI still signs itself in.
        let h2 = home("cfg-lo", Some("[web]\nauth_token = \"tok\"\n"));
        let lo = plan_launch(Some(&h2));
        assert_eq!((lo.host.as_str(), lo.webview_token.as_deref()), ("127.0.0.1", Some("tok")));
        let _ = std::fs::remove_dir_all(&h);
        let _ = std::fs::remove_dir_all(&h2);
    }

    #[test]
    fn no_token_means_no_lan() {
        // The token can't be written (its path is a directory): fail closed to
        // loopback instead of serving the LAN unauthenticated.
        let h = home("closed", Some("[federation]\nbind_lan = true\n"));
        std::fs::create_dir_all(h.join(DESKTOP_TOKEN_FILE)).unwrap();
        let plan = plan_launch(Some(&h));
        assert_eq!(plan.host, "127.0.0.1");
        assert_eq!(plan.sidecar_token, None);
        // No eVi home at all: same.
        assert_eq!(plan_launch(None).host, "127.0.0.1");
        let _ = std::fs::remove_dir_all(&h);
    }

    #[test]
    fn sidecar_env_carries_exactly_the_planned_token() {
        let h = home("env", Some("[federation]\nbind_lan = true\n"));
        let plan = plan_launch(Some(&h));
        let mut cmd = Command::new("x");
        apply_launch_env(&mut cmd, &plan);
        let envs: Vec<_> = cmd.get_envs().collect();
        assert_eq!(envs.len(), 1);
        assert_eq!(envs[0].0, TOKEN_ENV);
        assert_eq!(envs[0].1.and_then(|v| v.to_str()), plan.sidecar_token.as_deref());
        // Loopback: an inherited token is removed, not passed through.
        let mut cmd = Command::new("x");
        apply_launch_env(&mut cmd, &plan_launch(None));
        let envs: Vec<_> = cmd.get_envs().collect();
        assert_eq!(envs.len(), 1);
        assert_eq!((envs[0].0, envs[0].1), (std::ffi::OsStr::new(TOKEN_ENV), None));
        let _ = std::fs::remove_dir_all(&h);
    }

    #[test]
    fn init_script_guards_the_token_by_origin() {
        let origins = vec!["http://127.0.0.1:8473".to_string(), "http://localhost:8473".to_string()];
        let js = webview_init_script(Some(8473), Some("abc\"</script>"), &origins);
        assert!(js.starts_with("window.__EVI_PORT__ = 8473;"), "{js}");
        assert!(
            js.contains(r#"["http://127.0.0.1:8473","http://localhost:8473"].indexOf(window.location.origin)>=0"#),
            "{js}"
        );
        // JSON-encoded: the quote can't end the string literal.
        assert!(js.contains(r#"window.__EVI_TOKEN__="abc\"</script>";"#), "{js}");
        // No token: no __EVI_TOKEN__ at all; remote mode: no port.
        assert_eq!(webview_init_script(Some(1), None, &origins), "window.__EVI_PORT__ = 1;");
        assert_eq!(webview_init_script(Some(1), Some(""), &origins), "window.__EVI_PORT__ = 1;");
        assert!(!webview_init_script(None, Some("t"), &origins).contains("__EVI_PORT__"));
        assert_eq!(webview_init_script(None, None, &origins), "");
    }

    #[test]
    fn remote_token_comes_from_env_or_the_matching_peer_never_local_config() {
        let h = home(
            "remote",
            Some("[web]\nauth_token = \"this-machines-own\"\n"),
        );
        let remote = "http://gpu-box:8473/";
        // Nothing for that server: no token at all — not this machine's own.
        assert_eq!(remote_token(None, Some(&h), remote), None);
        std::fs::write(
            h.join("peers.json"),
            r#"[{"name":"other","url":"http://other:8473","token":"other-tok"},
                {"name":"gpu","url":"http://GPU-box:8473/","token":" gpu-tok "},
                {"name":"bad","url":"not a url","token":"x"}]"#,
        )
        .unwrap();
        // The peer at the same origin (host case and trailing slash don't matter).
        assert_eq!(remote_token(None, Some(&h), remote).as_deref(), Some("gpu-tok"));
        // EVI_REMOTE_TOKEN wins; a blank one is ignored.
        assert_eq!(remote_token(Some(" env ".into()), Some(&h), remote).as_deref(), Some("env"));
        assert_eq!(remote_token(Some("  ".into()), Some(&h), remote).as_deref(), Some("gpu-tok"));
        // Another port is another server.
        assert_eq!(remote_token(None, Some(&h), "http://gpu-box:9000"), None);
        assert_eq!(remote_token(None, None, remote), None);
        let _ = std::fs::remove_dir_all(&h);
    }

    #[test]
    fn remote_origin_is_scheme_host_port() {
        assert_eq!(url_origin("https://ai.lan:8443/x/y?z").as_deref(), Some("https://ai.lan:8443"));
        assert_eq!(url_origin("http://10.0.0.5:8473").as_deref(), Some("http://10.0.0.5:8473"));
        assert_eq!(url_origin("https://ai.example/").as_deref(), Some("https://ai.example"));
        assert_eq!(url_origin("not a url"), None);
        assert_eq!(url_origin("data:text/html,x"), None);
    }
}

#[cfg(test)]
mod ipc_guard_tests {
    // The main window renders model output, so what it can reach is pinned
    // here: the IPC capability names only the server's exact origin(s), and the
    // navigation guard keeps the window on them and on the bundled shim.
    use super::{command_permission, navigation_allowed, origin_pattern, UI_COMMANDS};
    use std::path::Path;
    use tauri::utils::acl::RemoteUrlPattern;

    fn url(s: &str) -> url::Url {
        url::Url::parse(s).unwrap()
    }

    fn matches(origin: &str, page: &str) -> bool {
        let pattern: RemoteUrlPattern = origin_pattern(origin)
            .unwrap_or_else(|| panic!("no pattern for {origin}"))
            .parse()
            .unwrap();
        pattern.test(&url(page))
    }

    #[test]
    fn capability_pattern_matches_only_its_own_origin() {
        let o = "http://127.0.0.1:8473";
        assert_eq!(origin_pattern(o).as_deref(), Some("http://127.0.0.1:8473"));
        for page in [
            "http://127.0.0.1:8473/",
            "http://127.0.0.1:8473/?_ui=123",
            "http://127.0.0.1:8473/static/index.html#x",
        ] {
            assert!(matches(o, page), "{page}");
        }
        // Another port on loopback — any local dev server or hostile local
        // page — is not the sidecar (the old `http://127.0.0.1:*` grant).
        for page in [
            "http://127.0.0.1:8474/",
            "http://127.0.0.1/",
            "https://127.0.0.1:8473/",
            "http://localhost:8473/",
            "http://127.0.0.2:8473/",
            "http://127.0.0.1.evil.example:8473/",
        ] {
            assert!(!matches(o, page), "{page}");
        }
    }

    #[test]
    fn remote_origin_patterns_cover_default_ports_and_ipv6() {
        assert!(matches("https://ai.example", "https://ai.example/x/y?z"));
        assert!(!matches("https://ai.example", "https://ai.example:8443/"));
        assert!(!matches("https://ai.example", "https://evil.ai.example/"));
        assert!(matches("http://gpu-box:8473", "http://GPU-box:8473/"));
        assert!(matches("http://[::1]:8000", "http://[::1]:8000/x"));
        assert!(!matches("http://[::1]:8000", "http://[::2]:8000/x"));
        // A `*` is legal in a WHATWG domain but must not become a wildcard.
        assert!(matches("http://a*b:1", "http://a*b:1/"));
        assert!(!matches("http://a*b:1", "http://axxb:1/"));
        for bad in ["", "not a url", "file:///C:/x", "data:text/html,x", "tauri://localhost"] {
            assert_eq!(origin_pattern(bad), None, "{bad:?}");
        }
    }

    #[test]
    fn ui_commands_are_the_ones_build_rs_declares() {
        // build.rs generates `allow-<cmd>` for each declared command; the
        // capability references exactly those, so a command added to one list
        // and not the other fails here instead of panicking at launch.
        let dir = Path::new(env!("CARGO_MANIFEST_DIR"));
        let build_rs = std::fs::read_to_string(dir.join("build.rs")).unwrap();
        let declared: Vec<&str> = build_rs
            .split('"')
            .skip(1)
            .step_by(2)
            .filter(|s| s.ends_with("_cmd"))
            .collect();
        let mut ours = UI_COMMANDS.to_vec();
        let mut theirs = declared.clone();
        ours.sort_unstable();
        theirs.sort_unstable();
        assert_eq!(ours, theirs);
        let generated = dir.join("permissions/autogenerated");
        for cmd in UI_COMMANDS {
            let toml = std::fs::read_to_string(generated.join(format!("{cmd}.toml"))).unwrap();
            assert!(
                toml.contains(&format!("identifier = \"{}\"", command_permission(cmd))),
                "{cmd}: {toml}"
            );
        }
    }

    #[test]
    fn navigation_stays_on_the_server_and_the_bundled_shim() {
        let origins = vec![
            "http://127.0.0.1:8473".to_string(),
            "http://localhost:8473".to_string(),
        ];
        for ok in [
            "http://127.0.0.1:8473/?_ui=1",
            "http://localhost:8473/?session=abc",
            "http://tauri.localhost/index.html",
            "https://tauri.localhost/index.html",
            "tauri://localhost/index.html",
            "blob:http://127.0.0.1:8473/6c1f9a54-0000-4000-8000-000000000000",
        ] {
            assert!(navigation_allowed(&url(ok), &origins), "{ok}");
        }
        for bad in [
            "https://evil.example/",
            "http://127.0.0.1:8474/",
            "http://127.0.0.1/",
            "http://tauri.localhost:8080/",
            "http://evil.tauri.localhost/",
            "tauri://evil/",
            "javascript:alert(1)",
            "data:text/html,<script>x</script>",
            "file:///C:/Windows/win.ini",
            "about:blank",
            "blob:https://evil.example/6c1f9a54-0000-4000-8000-000000000000",
            "evi://session/x",
        ] {
            assert!(!navigation_allowed(&url(bad), &origins), "{bad}");
        }
        // Thin-client mode: the remote server, not this machine's loopback.
        let remote = vec!["https://ai.example:8443".to_string()];
        assert!(navigation_allowed(&url("https://ai.example:8443/x"), &remote));
        assert!(!navigation_allowed(&url("http://127.0.0.1:8473/"), &remote));
    }
}

#[cfg(test)]
mod external_url_tests {
    // `open_external_cmd` is invokable from the webview, which renders model
    // output, so `validate_external_url` is the gate between a crafted link and
    // the OS. The other half of the fix (no shell ever parses the URL) is
    // pinned by inspecting the unspawned `url_open_command` / `dir_open_command`
    // for every OS, so even Linux CI catches a Windows revert to cmd.exe.
    use super::{
        dir_open_command, url_open_command, validate_external_url, windows_root_from,
        windows_system_exe, Opener,
    };
    use std::path::Path;
    use std::process::Command;

    fn ok(raw: &str) -> String {
        validate_external_url(raw)
            .unwrap_or_else(|e| panic!("{raw:?} rejected: {e}"))
    }

    fn rejected(raw: &str) -> bool {
        validate_external_url(raw).is_err()
    }

    /// Program followed by its arguments, as the OS will receive them.
    fn argv(cmd: &Command) -> Vec<String> {
        std::iter::once(cmd.get_program())
            .chain(cmd.get_args())
            .map(|s| s.to_string_lossy().into_owned())
            .collect()
    }

    #[test]
    fn accepts_normal_http_and_https_urls() {
        assert_eq!(
            ok("https://github.com/evi-assistant/evi-ai/issues"),
            "https://github.com/evi-assistant/evi-ai/issues"
        );
        // `&` and `%` are everyday URL characters and must survive untouched —
        // they were only dangerous because cmd.exe re-parsed them.
        assert_eq!(
            ok("http://example.com/a?b=1&c=2#frag"),
            "http://example.com/a?b=1&c=2#frag"
        );
        assert_eq!(
            ok("https://example.com/search?q=a%20b&x=%26y"),
            "https://example.com/search?q=a%20b&x=%26y"
        );
        assert_eq!(
            ok("https://example.com/&calc.exe"),
            "https://example.com/&calc.exe"
        );
        assert_eq!(ok("http://127.0.0.1:8473/x"), "http://127.0.0.1:8473/x");
        assert_eq!(ok("http://[::1]:8080/"), "http://[::1]:8080/");
    }

    #[test]
    fn returns_the_normalized_serialization() {
        assert_eq!(ok("https://example.com"), "https://example.com/");
        assert_eq!(ok("HTTPS://Example.COM/Path"), "https://example.com/Path");
        assert_eq!(ok("https://example.com:443/"), "https://example.com/");
        assert_eq!(
            ok("https://bücher.example/"),
            "https://xn--bcher-kva.example/"
        );
    }

    #[test]
    fn shell_metacharacters_in_the_host_are_rejected() {
        // Real `url` behaviour: `&` is not a WHATWG forbidden domain code point,
        // so the crate itself accepts `x&calc.exe` as a host...
        assert_eq!(
            url::Url::parse("http://x&calc.exe").unwrap().host_str(),
            Some("x&calc.exe")
        );
        // ...and it's our host allowlist that refuses it.
        assert!(rejected("http://x&calc.exe"));
        assert!(rejected("http://x|calc.exe/"));
        assert!(rejected("http://a%26calc.exe/")); // host percent-decodes to a&calc.exe
        assert!(rejected("http://a%22b/")); // host percent-decodes to a"b
        assert!(rejected("http://a'b/")); // quotes are only encoded outside the host
        assert!(rejected("http://a`b/"));
    }

    #[test]
    fn non_http_schemes_are_rejected() {
        for raw in [
            "file:///C:/Windows",
            "file:///C:/Windows/System32/calc.exe",
            "javascript:alert(1)",
            "ms-settings:",
            "mailto:someone@example.com",
            "ftp://example.com/",
            "data:text/html,hi",
            "evi://session/x",
            "C:\\Windows\\System32\\calc.exe",
            "calc.exe",
        ] {
            assert!(rejected(raw), "{raw:?} should be rejected");
        }
    }

    #[test]
    fn whitespace_and_control_chars_are_rejected() {
        for raw in [
            "https://example.com/a b",
            "https://exa mple.com/",
            " https://example.com/",
            "https://example.com/\n",
            "https://example.com/\r\ncalc.exe",
            "https://exam\tple.com/",
            "https://example.com/\u{0}",
            "https://example.com/\u{7f}",
            "https://example.com/\u{85}",
            "https://example.com/\u{a0}",
            "https://example.com/\u{2028}",
        ] {
            assert!(rejected(raw), "{raw:?} should be rejected");
        }
    }

    #[test]
    fn empty_and_hostless_inputs_are_rejected() {
        assert!(rejected(""));
        assert!(rejected("http://"));
        assert!(rejected("https://"));
        assert!(rejected("//example.com/"));
        assert!(rejected("not a url"));
    }

    #[test]
    fn embedded_credentials_are_rejected() {
        for raw in [
            "https://github.com@evil.com/", // opens evil.com
            "https://user:pass@evil.com/",
            "https://:pass@evil.com/",
            "http://user@127.0.0.1:8473/",
        ] {
            assert!(rejected(raw), "{raw:?} should be rejected");
        }
    }

    #[test]
    fn quotes_are_percent_encoded_never_passed_through() {
        // Raw `'` is legal in real links; it's encoded, not refused (and not
        // left raw for the OS opener).
        assert_eq!(
            ok("https://en.wikipedia.org/wiki/Moore's_law"),
            "https://en.wikipedia.org/wiki/Moore%27s_law"
        );
        assert_eq!(
            ok("https://en.wikipedia.org/wiki/Moore%27s_law"),
            "https://en.wikipedia.org/wiki/Moore%27s_law"
        );
        assert_eq!(
            ok("https://example.com/\"&calc.exe"),
            "https://example.com/%22&calc.exe"
        );
        // Every quote kind in path, query and fragment.
        let out = ok("https://example.com/p`a\"t'h?q=\"a\"&r='b'&s=`c`#f'r`a\"g");
        assert_eq!(
            out,
            "https://example.com/p%60a%22t%27h?q=%22a%22&r=%27b%27&s=%60c%60#f%27r%60a%22g"
        );
        assert!(!out.contains(['"', '\'', '`']), "{out:?}");
    }

    #[test]
    fn url_goes_to_a_non_shell_opener_as_one_verbatim_argument() {
        // The core of the fix: reverting Windows to `cmd /C start "" <url>`
        // (which let `http://x&calc.exe` run calc) fails here on any host OS,
        // since every OS's argv is built and checked, not just the host's.
        let url = ok("https://example.com/?a=1&b=2|c^d");
        assert_eq!(url, "https://example.com/?a=1&b=2|c^d");
        let u = url.as_str();
        let rundll32 = windows_system_exe(r"System32\rundll32.exe");
        assert_eq!(
            argv(&url_open_command(Opener::Windows, u)),
            [rundll32.as_str(), "url.dll,FileProtocolHandler", u]
        );
        assert_eq!(argv(&url_open_command(Opener::MacOs, u)), ["open", u]);
        assert_eq!(argv(&url_open_command(Opener::Xdg, u)), ["xdg-open", u]);
    }

    #[test]
    fn dir_goes_to_a_non_shell_opener_as_one_argument() {
        // A profile path like `Tom & Jerry` must not be re-parsed by cmd.exe.
        let dir = "C:/Users/Tom & Jerry/.evi/logs";
        let explorer = windows_system_exe("explorer.exe");
        for (os, opener) in [
            (Opener::Windows, explorer.as_str()),
            (Opener::MacOs, "open"),
            (Opener::Xdg, "xdg-open"),
        ] {
            assert_eq!(argv(&dir_open_command(os, Path::new(dir))), [opener, dir]);
        }
    }

    #[test]
    fn windows_openers_run_by_absolute_system_path() {
        // A bare `rundll32.exe` / `explorer.exe` is looked up in the app's own
        // folder first, so a binary planted next to evi-desktop.exe would run.
        let is_abs = |p: &str| {
            let b = p.as_bytes();
            b.len() > 3 && b[0].is_ascii_alphabetic() && b[1] == b':' && b[2] == b'\\'
        };
        let rundll32 = argv(&url_open_command(Opener::Windows, "https://example.com/"))[0].clone();
        assert!(is_abs(&rundll32), "{rundll32}");
        assert!(rundll32.to_ascii_lowercase().ends_with(r"\system32\rundll32.exe"), "{rundll32}");
        let explorer = argv(&dir_open_command(Opener::Windows, Path::new("C:/x")))[0].clone();
        assert!(is_abs(&explorer), "{explorer}");
        assert!(explorer.to_ascii_lowercase().ends_with(r"\explorer.exe"), "{explorer}");
        assert!(!explorer.to_ascii_lowercase().contains("system32"), "{explorer}");
    }

    #[test]
    fn windows_root_takes_only_an_absolute_drive_path() {
        assert_eq!(windows_root_from(Some(r"C:\WINDOWS".into())), r"C:\WINDOWS");
        assert_eq!(windows_root_from(Some(r"D:\Win\".into())), r"D:\Win");
        assert_eq!(windows_root_from(Some("E:/Windows/".into())), "E:/Windows");
        // Relative, empty, UNC-less junk or unset: the stock location, never a
        // name the search path would resolve.
        for bad in ["", "Windows", r"\Windows", r"..\x", "C:", "C:Windows"] {
            assert_eq!(windows_root_from(Some(bad.into())), r"C:\Windows", "{bad:?}");
        }
        assert_eq!(windows_root_from(None), r"C:\Windows");
    }

    #[test]
    fn the_host_opener_matches_the_build_target() {
        let want = if cfg!(windows) {
            Opener::Windows
        } else if cfg!(target_os = "macos") {
            Opener::MacOs
        } else {
            Opener::Xdg
        };
        assert_eq!(Opener::host(), want);
    }
}
