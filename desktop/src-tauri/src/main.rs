// Prevents the extra console window on Windows release builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};

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

/// Directory for the spawned server's log, under the Evi home dir. Created
/// on demand; None if it can't be set up.
fn server_log_dir() -> Option<PathBuf> {
    let base = std::env::var("EVI_HOME").ok().map(PathBuf::from).or_else(|| {
        std::env::var(if cfg!(windows) { "USERPROFILE" } else { "HOME" })
            .ok()
            .map(|h| PathBuf::from(h).join(".evi"))
    })?;
    let dir = base.join("logs");
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

    let mut cmd = Command::new(program);
    cmd.args(prefix_args)
        .args([
            "uvicorn",
            "evi.apps.web.server:app",
            "--host",
            "127.0.0.1",
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
            "127.0.0.1",
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
    let mut cmd = Command::new(path);
    cmd.args(["--host", "127.0.0.1", "--port", &port.to_string()]);

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

fn main() {
    let server = ServerHandle(Mutex::new(None));

    let app = tauri::Builder::default()
        .manage(server)
        .setup(|app| {
            // `local_port` is Some in local mode; the loading shim polls that
            // port and redirects once the server is up.
            let mut local_port: Option<u16> = None;

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
                WebviewUrl::External(trimmed.parse().map_err(|e: url::ParseError| e.to_string())?)
            } else {
                // Local mode. Prefer a bundled sidecar (standalone build);
                // otherwise fall back to spawning a system Python from the
                // repo root (developer / source-checkout install).
                let port = pick_free_port()?;
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

                // Show the loading shim immediately — do NOT block here waiting
                // for health. The onefile sidecar's cold start (unpacking +
                // importing) is slow and variable; the shim polls the port and
                // redirects when ready, so the user never sees a refused page.
                WebviewUrl::App("index.html".into())
            };

            let mut builder = WebviewWindowBuilder::new(app, "main", target)
                .title("Evi")
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
