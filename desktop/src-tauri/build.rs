fn main() {
    warn_if_webview2_loader_unbundled();
    // Declare our app commands so tauri-build generates their ACL permissions
    // (allow-<cmd> / deny-<cmd>). Required because the UI loads from a REMOTE
    // origin (the local web server) — remote origins only get a capability's
    // listed permissions, and the bare tauri_build::build() generates none for
    // app commands, so they'd be blocked by the ACL. See capabilities/default.json.
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(
            tauri_build::AppManifest::new().commands(&[
                "check_for_update_cmd",
                "install_update_cmd",
                "restart_app_cmd",
                "update_available_cmd",
                "dismiss_update_cmd",
                "staged_update_cmd",
                "open_logs_cmd",
                "open_external_cmd",
                "update_status_cmd",
            ]),
        ),
    )
    .expect("failed to run tauri-build");
}

/// A Windows GNU-toolchain exe loads WebView2Loader.dll at runtime (MSVC links
/// it in). tauri-build copies it next to the exe in target/, but the bundler
/// doesn't pack it, so an installer built without the overlay that
/// scripts/stage_webview2_loader.py stages fails on launch. Warn on release
/// builds whose `tauri build --config` (TAURI_CONFIG) doesn't bundle it.
fn warn_if_webview2_loader_unbundled() {
    println!("cargo:rerun-if-env-changed=TAURI_CONFIG");
    let os = std::env::var("CARGO_CFG_TARGET_OS").unwrap_or_default();
    let abi = std::env::var("CARGO_CFG_TARGET_ENV").unwrap_or_default();
    let release = std::env::var("PROFILE").map(|p| p == "release").unwrap_or(false);
    let bundled = std::env::var("TAURI_CONFIG")
        .map(|c| c.contains("WebView2Loader.dll"))
        .unwrap_or(false);
    if os == "windows" && abi == "gnu" && release && !bundled {
        println!(
            "cargo:warning=GNU toolchain: an installer from this build needs WebView2Loader.dll \
             next to the exe. Build with scripts/build-desktop.ps1, or pass the --config that \
             `python scripts/stage_webview2_loader.py` prints to `tauri build`."
        );
    }
}
