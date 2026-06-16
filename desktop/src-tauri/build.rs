fn main() {
    // Declare our app commands so tauri-build generates their ACL permissions
    // (allow-<cmd> / deny-<cmd>). Required because the UI loads from a REMOTE
    // origin (the local web server) — remote origins only get a capability's
    // listed permissions, and the bare tauri_build::build() generates none for
    // app commands, so they'd be blocked by the ACL. See capabilities/default.json.
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(
            tauri_build::AppManifest::new().commands(&[
                "check_for_update_cmd",
                "open_logs_cmd",
                "open_external_cmd",
                "update_status_cmd",
            ]),
        ),
    )
    .expect("failed to run tauri-build");
}
