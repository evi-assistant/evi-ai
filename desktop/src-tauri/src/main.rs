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

/// Host to bind the spawned server to. Loopback by default; `[federation]
/// bind_lan = true` in config.toml exposes it on the LAN so this instance can
/// act as a federation peer. The webview still connects via 127.0.0.1.
fn server_bind_host() -> String {
    let lan = evi_home()
        .map(|h| h.join("config.toml"))
        .and_then(|p| std::fs::read_to_string(p).ok())
        .and_then(|t| t.parse::<toml::Value>().ok())
        .and_then(|v| {
            v.get("federation")
                .and_then(|f| f.get("bind_lan"))
                .and_then(|b| b.as_bool())
        })
        .unwrap_or(false);
    if lan { "0.0.0.0".to_string() } else { "127.0.0.1".to_string() }
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
/// On Windows we prefer the `py -3.11` launcher; elsewhere fall back to `python3`.
fn spawn_server(repo_root: &Path, port: u16) -> std::io::Result<Child> {
    let (program, prefix_args): (&str, &[&str]) = if cfg!(windows) {
        ("py", &["-3.11", "-m"])
    } else {
        ("python3", &["-m"])
    };

    let host = server_bind_host();
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
fn spawn_sidecar(path: &Path, port: u16) -> std::io::Result<Child> {
    let host = server_bind_host();
    let mut cmd = Command::new(path);
    cmd.args(["--host", &host, "--port", &port.to_string()]);

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
    while Instant::now() < deadline {
        if let Ok(resp) = ureq::get(&url).timeout(Duration::from_millis(500)).call() {
            if resp.status() >= 200 && resp.status() < 300 {
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
                eprintln!("evi: update {} available — downloading…", update.version);
                install_and_restart(handle.clone(), update).await;
            }
            Ok(None) => eprintln!("evi: already up to date"),
            Err(e) => eprintln!("evi: update check failed: {e}"),
        }
    });
}

/// Stop the sidecar, then download + install an update and relaunch. Shared by
/// the launch-time auto-check and Help → Check for Updates. The sidecar MUST
/// die first: the NSIS updater overwrites its onedir files (e.g.
/// _internal/VCRUNTIME140.dll), which Windows keeps locked while evi-server
/// runs — otherwise the installer fails with "Error opening file for writing".
async fn install_and_restart(handle: tauri::AppHandle, update: tauri_plugin_updater::Update) {
    let version = update.version.clone();
    // Announce the (silent) update so the webview can show a progress toast.
    set_update(&handle, "downloading", &version);
    if let Some(w) = handle.get_webview_window("main") {
        let _ = w.eval(
            "window.eviUI && window.eviUI.onUpdateStarted && window.eviUI.onUpdateStarted()",
        );
    }
    if let Some(mut child) = handle.state::<ServerHandle>().0.lock().unwrap().take() {
        let _ = child.kill();
        let _ = child.wait();
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
    match update.download_and_install(on_chunk, || {}).await {
        Ok(_) => {
            set_update(&handle, "installing", &version);
            eprintln!("evi: update installed — restarting");
            handle.restart();
        }
        Err(e) => {
            set_update(&handle, "error", &version);
            eprintln!("evi: update install failed: {e}");
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

/// Help → Check for Updates. Returns the verdict immediately; when an update
/// exists the download + install proceeds in the background and the app
/// relaunches when it lands. The EVI_AUTO_UPDATE opt-out does not gate this —
/// the user asked explicitly.
#[tauri::command]
async fn check_for_update_cmd(app: tauri::AppHandle) -> Result<UpdateStatus, String> {
    use tauri_plugin_updater::UpdaterExt;
    let current = app.package_info().version.to_string();
    let updater = app.updater().map_err(|e| e.to_string())?;
    match updater.check().await {
        Ok(Some(update)) => {
            let version = update.version.clone();
            tauri::async_runtime::spawn(install_and_restart(app.clone(), update));
            Ok(UpdateStatus { available: true, version, current })
        }
        Ok(None) => Ok(UpdateStatus { available: false, version: String::new(), current }),
        Err(e) => Err(e.to_string()),
    }
}

/// Open the eVi logs folder in the OS file manager (Help → Open Logs Folder).
#[tauri::command]
fn open_logs_cmd() -> Result<(), String> {
    match server_log_dir() {
        Some(dir) => os_open(&dir.to_string_lossy()).map_err(|e| e.to_string()),
        None => Err("no log directory".into()),
    }
}

/// Open an external http(s) URL in the default browser. Restricted to http/https
/// so a stray invoke can't launch arbitrary local programs.
#[tauri::command]
fn open_external_cmd(url: String) -> Result<(), String> {
    if !(url.starts_with("http://") || url.starts_with("https://")) {
        return Err("only http(s) URLs are allowed".into());
    }
    os_open(&url).map_err(|e| e.to_string())
}

/// Hand a URL or path to the OS to open with its default handler.
fn os_open(target: &str) -> std::io::Result<()> {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        let mut c = Command::new("cmd");
        c.args(["/C", "start", "", target]).creation_flags(0x0800_0000);
        c.spawn().map(|_| ())
    }
    #[cfg(target_os = "macos")]
    {
        Command::new("open").arg(target).spawn().map(|_| ())
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        Command::new("xdg-open").arg(target).spawn().map(|_| ())
    }
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
    let devtools = MenuItemBuilder::with_id("toggle_devtools", "Toggle Developer Tools")
        .accelerator("CmdOrCtrl+Shift+I")
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
/// `eval` rather than `emit` because the window loads a remote http:// URL (the
/// local server) where withGlobalTauri's event module isn't reliably injected —
/// so an emitted event may have no listener. eval always reaches the page.
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
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_deep_link::init())
        .invoke_handler(tauri::generate_handler![
            check_for_update_cmd,
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
                WebviewUrl::External(trimmed.parse().map_err(|e: url::ParseError| e.to_string())?)
            } else {
                // Local mode. Prefer a bundled sidecar (standalone build);
                // otherwise fall back to spawning a system Python from the
                // repo root (developer / source-checkout install).
                let port = pick_port()?;
                let child = if let Some(side) = sidecar_path(app) {
                    spawn_sidecar(&side, port)?
                } else {
                    let exe = std::env::current_exe()?;
                    let start = exe.parent().unwrap_or(Path::new("."));
                    let repo_root = std::env::var("EVI_REPO_ROOT")
                        .ok()
                        .map(PathBuf::from)
                        .or_else(|| find_repo_root(start))
                        .ok_or("could not locate evi repo root (set EVI_REPO_ROOT) \
                                and no bundled sidecar found")?;
                    spawn_server(&repo_root, port)?
                };
                app.state::<ServerHandle>().0.lock().unwrap().replace(child);
                local_port = Some(port);
                nav_base = Some(format!("http://127.0.0.1:{}", port));

                // Show the loading shim immediately — do NOT block here waiting
                // for health. The onefile sidecar's cold start (unpacking +
                // importing) is slow and variable; the shim polls the port and
                // redirects when ready, so the user never sees a refused page.
                WebviewUrl::App("index.html".into())
            };

            let mut builder = WebviewWindowBuilder::new(app, "main", target)
                .title("eVi")
                .inner_size(1100.0, 800.0)
                .min_inner_size(600.0, 400.0)
                .resizable(true);
            if let Some(port) = local_port {
                builder = builder
                    .initialization_script(format!("window.__EVI_PORT__ = {};", port));
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
