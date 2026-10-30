//! Sidecar update channel — decouple *core* updates from the Tauri shell.
//!
//! The frozen Python sidecar (`evi-server`) is the part that changes every core
//! release; the Rust shell rarely changes. Rather than rebuild + re-ship the
//! whole app for a core update, we fetch a small signed manifest and, if a newer
//! ABI-compatible sidecar exists, download + verify + stage it into a writable
//! dir. `main.rs` prefers a staged sidecar over the bundled one, so the update
//! takes effect on the **next launch** (no mid-session restart). Any failure
//! keeps the bundled last-known-good — the app is never left without a sidecar.
//!
//! Trust: the manifest is minisign-signed with the SAME key as the Tauri app
//! updater (`PUBKEY` below is that key), and each zip is checked against the
//! sha256 the signed manifest pins. So a tampered release or a MITM can't stage
//! a bad sidecar.

use std::io::Read;
use std::path::{Path, PathBuf};

/// The shell↔sidecar launch contract version. A staged sidecar is only used when
/// the manifest's `min_shell_abi` <= this. Bump ONLY on a breaking change to how
/// the shell launches/handshakes with the sidecar (flags, ports, `--check`), so
/// an old shell refuses a sidecar it can't drive and keeps its bundled one.
///
/// 1 — `--host/--port`, `--check`.
/// 2 — with `[federation] bind_lan` the shell hands the sidecar an access token
///     (`EVI_WEB_AUTH_TOKEN`) and injects it into the webview. A core that
///     answers only loopback clients without a token would leave an ABI-1
///     shell's LAN setup locked out, so such cores ship `min_shell_abi = 2`
///     and ABI-1 shells get them only through a full app update.
pub const SHELL_ABI: u32 = 2;

const TAG_BASE: &str =
    "https://github.com/evi-assistant/evi-ai/releases/download/sidecar-latest";

/// minisign public key — the raw key line from the Tauri updater key
/// (tauri.conf.json `plugins.updater.pubkey`, base64-decoded to its 2nd line).
const PUBKEY: &str = "RWT8Uf8QR6hFQubK7o3kct+34TZoKRggjn7d40yq6dfQXeM3QljyoOoq";

#[derive(serde::Deserialize)]
struct Manifest {
    version: String,
    min_shell_abi: u32,
    platforms: std::collections::HashMap<String, PlatformEntry>,
}

#[derive(serde::Deserialize)]
struct PlatformEntry {
    url: String,
    sha256: String,
}

/// This build's platform key (matches scripts/build-sidecar-manifest.py).
fn platform_key() -> Option<&'static str> {
    Some(match (std::env::consts::OS, std::env::consts::ARCH) {
        ("windows", "x86_64") => "windows-x86_64",
        ("macos", "aarch64") => "darwin-aarch64",
        ("macos", "x86_64") => "darwin-x86_64",
        ("linux", "x86_64") => "linux-x86_64",
        _ => return None,
    })
}

fn sidecar_bin() -> &'static str {
    if cfg!(windows) { "evi-server.exe" } else { "evi-server" }
}

fn root(evi_home: &Path) -> PathBuf {
    evi_home.join("sidecar")
}

fn active_version(evi_home: &Path) -> Option<String> {
    let v = std::fs::read_to_string(root(evi_home).join("active")).ok()?;
    let v = v.trim().to_string();
    if v.is_empty() { None } else { Some(v) }
}

/// The staged sidecar binary to prefer over the bundled one, if a compatible one
/// was downloaded on a previous run. `None` → caller uses the bundled sidecar.
///
/// Guard: only prefer the staged core when it is at least as new as the bundled
/// one. A full-app update ships a fresh bundled sidecar; without this check a
/// stale staged version (from the sidecar channel) would shadow it forever — the
/// app would report the old core version even after a full upgrade until the
/// channel happened to re-stage. Comparing versions makes a full update win.
pub fn staged_sidecar(evi_home: &Path, bundled_version: &str) -> Option<PathBuf> {
    let ver = active_version(evi_home)?;
    if parse_version(&ver) < parse_version(bundled_version) {
        return None;
    }
    let p = root(evi_home).join(&ver).join("evi-server").join(sidecar_bin());
    if p.is_file() { Some(p) } else { None }
}

/// Parse "X.Y.Z" (ignoring any -pre/+build suffix) into a comparable tuple.
fn parse_version(v: &str) -> (u64, u64, u64) {
    let core = v.split(['-', '+']).next().unwrap_or(v);
    let mut it = core.split('.').map(|x| x.parse::<u64>().unwrap_or(0));
    (it.next().unwrap_or(0), it.next().unwrap_or(0), it.next().unwrap_or(0))
}

fn is_newer(candidate: &str, current: &str) -> bool {
    parse_version(candidate) > parse_version(current)
}

/// Whether a shell speaking `shell_abi` can drive a core whose manifest asks
/// for `min_shell_abi`. An older shell keeps its bundled core instead.
fn shell_can_run(min_shell_abi: u32, shell_abi: u32) -> bool {
    min_shell_abi <= shell_abi
}

/// The core version actually in effect, matching `staged_sidecar`'s guard: a
/// staged (`active`) core only wins when it is at least as new as the bundled
/// one, otherwise the bundle runs. Used so the update check doesn't stage a core
/// the guard would then reject (which would raise a misleading "Restart now" and
/// waste a download).
fn effective_current(active: Option<String>, bundled: &str) -> String {
    match active {
        Some(v) if parse_version(&v) >= parse_version(bundled) => v,
        _ => bundled.to_string(),
    }
}

/// Verify the manifest signature over `data` with our embedded public key.
/// `sig_b64` is the `.sig` produced by `tauri signer sign` — base64 of the raw
/// minisign `.minisig` file text (same key/format as the Tauri app updater).
fn verify_signature(data: &[u8], sig_b64: &str) -> bool {
    verify_signature_with(PUBKEY, data, sig_b64)
}

/// `verify_signature` against an explicit public key (tests use a known vector).
fn verify_signature_with(pubkey_b64: &str, data: &[u8], sig_b64: &str) -> bool {
    use base64::Engine;
    use minisign_verify::{PublicKey, Signature};
    let decoded = match base64::engine::general_purpose::STANDARD.decode(sig_b64.trim()) {
        Ok(b) => b,
        Err(_) => return false,
    };
    let sig_text = match std::str::from_utf8(&decoded) {
        Ok(s) => s,
        Err(_) => return false,
    };
    let pk = match PublicKey::from_base64(pubkey_b64) {
        Ok(p) => p,
        Err(_) => return false,
    };
    let sig = match Signature::decode(sig_text) {
        Ok(s) => s,
        Err(_) => return false,
    };
    pk.verify(data, &sig, false).is_ok()
}

fn sha256_hex(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    let mut h = Sha256::new();
    h.update(bytes);
    let digest = h.finalize();
    let mut s = String::with_capacity(64);
    for b in digest {
        s.push_str(&format!("{:02x}", b));
    }
    s
}

/// Blocking GET returning the response body as bytes (ureq follows redirects).
/// The explicit limit matters: ureq 3's body readers cap at a small default, and
/// the sidecar zip is tens of MB — without it a valid download would truncate.
fn http_get_bytes(url: &str) -> Option<Vec<u8>> {
    let mut resp = ureq::get(url).call().ok()?;
    let mut buf = Vec::new();
    resp.body_mut()
        .as_reader()
        .take(512 * 1024 * 1024)
        .read_to_end(&mut buf)
        .ok()?;
    Some(buf)
}

fn http_get_string(url: &str) -> Option<String> {
    let mut resp = ureq::get(url).call().ok()?;
    resp.body_mut().read_to_string().ok()
}

/// Extract a zip (the `evi-server` onedir at its root) into `dest`.
fn extract_zip(bytes: &[u8], dest: &Path) -> std::io::Result<()> {
    let reader = std::io::Cursor::new(bytes);
    let mut zip = zip::ZipArchive::new(reader)
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
    for i in 0..zip.len() {
        let mut entry = zip
            .by_index(i)
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
        // enclosed_name() rejects path traversal (../, absolute) — skip anything unsafe.
        let rel = match entry.enclosed_name() {
            Some(p) => p.to_path_buf(),
            None => continue,
        };
        let out = dest.join(rel);
        if entry.is_dir() {
            std::fs::create_dir_all(&out)?;
        } else {
            if let Some(parent) = out.parent() {
                std::fs::create_dir_all(parent)?;
            }
            let mut f = std::fs::File::create(&out)?;
            std::io::copy(&mut entry, &mut f)?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                // rwx only: never carry setuid/setgid/sticky bits out of an archive.
                if let Some(mode) = entry.unix_mode() {
                    let _ = std::fs::set_permissions(&out, std::fs::Permissions::from_mode(mode & 0o777));
                }
            }
        }
    }
    Ok(())
}

/// sha256-check a downloaded sidecar zip against the digest the signed manifest
/// pins, then extract it into `<root>/<version>`. `None` on any failure, with
/// nothing left behind.
fn stage_archive(evi_home: &Path, version: &str, zip_bytes: &[u8], sha256: &str) -> Option<()> {
    if sha256_hex(zip_bytes) != sha256.to_lowercase() {
        eprintln!("evi: sidecar zip sha256 mismatch — ignoring");
        return None;
    }
    let dest = root(evi_home).join(version);
    // Extract into a temp dir first, then rename into place (atomic-ish).
    let tmp = root(evi_home).join(format!(".staging-{version}"));
    let _ = std::fs::remove_dir_all(&tmp);
    std::fs::create_dir_all(&tmp).ok()?;
    if extract_zip(zip_bytes, &tmp).is_err() {
        let _ = std::fs::remove_dir_all(&tmp);
        return None;
    }
    let _ = std::fs::remove_dir_all(&dest);
    if std::fs::rename(&tmp, &dest).is_err() {
        let _ = std::fs::remove_dir_all(&tmp);
        return None;
    }
    Some(())
}

/// Remove staged version dirs other than `keep` (best-effort housekeeping).
fn prune_old(evi_home: &Path, keep: &str) {
    if let Ok(entries) = std::fs::read_dir(root(evi_home)) {
        for e in entries.flatten() {
            let p = e.path();
            if p.is_dir() && p.file_name().and_then(|n| n.to_str()) != Some(keep) {
                let _ = std::fs::remove_dir_all(&p);
            }
        }
    }
}

/// Core (blocking) update step. Fetch + verify the manifest; if a newer,
/// ABI-compatible sidecar exists for this platform and isn't already staged,
/// download + sha256-check + extract it, run `--check`, and on success flip the
/// `active` pointer. Returns the staged version, or None if nothing was staged.
fn check_and_stage(evi_home: &Path, bundled_version: &str) -> Option<String> {
    let plat = platform_key()?;

    let manifest_text = http_get_string(&format!("{TAG_BASE}/sidecar-latest.json"))?;
    let sig = http_get_string(&format!("{TAG_BASE}/sidecar-latest.json.sig"))?;
    if !verify_signature(manifest_text.as_bytes(), &sig) {
        eprintln!("evi: sidecar manifest signature invalid — ignoring");
        return None;
    }
    let manifest: Manifest = serde_json::from_str(&manifest_text).ok()?;

    if !shell_can_run(manifest.min_shell_abi, SHELL_ABI) {
        // Needs a newer shell than this one — a full app update will bring it.
        return None;
    }
    // The version currently in effect: a staged core wins over the bundle only
    // when it's at least as new (mirrors `staged_sidecar`'s guard). A stale
    // `active` pointer older than a freshly-updated bundle must NOT be treated as
    // current, or we'd stage a core the guard rejects.
    let current = effective_current(active_version(evi_home), bundled_version);
    if !is_newer(&manifest.version, &current) {
        return None;
    }
    let entry = manifest.platforms.get(plat)?;

    let dest = root(evi_home).join(&manifest.version);
    // Already fully staged from a prior run (but active not yet flipped)? re-verify by presence.
    let bin = dest.join("evi-server").join(sidecar_bin());
    if !bin.is_file() {
        let zip_bytes = http_get_bytes(&entry.url)?;
        stage_archive(evi_home, &manifest.version, &zip_bytes, &entry.sha256)?;
    }

    // Prove the new sidecar actually runs before making it active (rollback = do nothing).
    if !sidecar_selfcheck(&bin) {
        eprintln!("evi: staged sidecar {} failed --check — keeping current", manifest.version);
        let _ = std::fs::remove_dir_all(&dest);
        return None;
    }

    // Flip the pointer, then prune older staged versions.
    if std::fs::write(root(evi_home).join("active"), &manifest.version).is_err() {
        return None;
    }
    prune_old(evi_home, &manifest.version);
    Some(manifest.version)
}

/// Run `<bin> --check` and treat exit 0 as healthy.
fn sidecar_selfcheck(bin: &Path) -> bool {
    let mut cmd = std::process::Command::new(bin);
    cmd.arg("--check");
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    matches!(cmd.status(), Ok(s) if s.success())
}

/// Read `[desktop] sidecar_auto_update` from `<evi_home>/config.toml`. Defaults
/// to `true` — an absent file / section / key means auto-update is on. The web
/// UI writes this key; the shell only reads it (like `federation.bind_lan`).
fn config_auto_update_enabled(evi_home: &Path) -> bool {
    let text = match std::fs::read_to_string(evi_home.join("config.toml")) {
        Ok(t) => t,
        Err(_) => return true,
    };
    text.parse::<toml::Table>()
        .ok()
        .and_then(|v| {
            v.get("desktop")
                .and_then(|d| d.get("sidecar_auto_update"))
                .and_then(|b| b.as_bool())
        })
        .unwrap_or(true)
}

/// Kick off a background sidecar-update check. Never blocks launch; opt out with
/// the `EVI_SIDECAR_UPDATE=0` env var (forces off) or the `[desktop]
/// sidecar_auto_update = false` setting. `bundled_version` is the app/bundled
/// sidecar version. `on_staged` is called with the version when a newer core is
/// staged (the shell uses it to offer a one-click "Restart now").
pub fn spawn_check<F>(evi_home: PathBuf, bundled_version: String, on_staged: F)
where
    F: Fn(String) + Send + 'static,
{
    let env_off = std::env::var("EVI_SIDECAR_UPDATE")
        .map(|v| v == "0" || v.eq_ignore_ascii_case("false"))
        .unwrap_or(false);
    if env_off || !config_auto_update_enabled(&evi_home) {
        return;
    }
    std::thread::spawn(move || {
        if let Some(v) = check_and_stage(&evi_home, &bundled_version) {
            eprintln!("evi: staged sidecar {v} — active on next launch");
            on_staged(v);
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn version_ordering() {
        assert!(is_newer("1.0.10", "1.0.9"));
        assert!(is_newer("1.1.0", "1.0.99"));
        assert!(!is_newer("1.0.9", "1.0.9"));
        assert!(!is_newer("1.0.8", "1.0.9"));
        assert!(is_newer("1.0.10-rc1", "1.0.9")); // suffix ignored
    }

    #[test]
    fn manifest_parses() {
        let m: Manifest = serde_json::from_str(
            r#"{"version":"1.0.10","min_shell_abi":1,
                "platforms":{"linux-x86_64":{"url":"http://x/z.zip","sha256":"ab"}}}"#,
        )
        .unwrap();
        assert_eq!(m.version, "1.0.10");
        assert_eq!(m.min_shell_abi, 1);
        assert!(m.platforms.contains_key("linux-x86_64"));
    }

    #[test]
    fn token_cores_are_offered_only_to_abi2_shells() {
        // This shell speaks the token handshake (SHELL_ABI 2)...
        assert!(SHELL_ABI >= 2);
        let m: Manifest = serde_json::from_str(
            r#"{"version":"1.0.54","min_shell_abi":2,
                "platforms":{"linux-x86_64":{"url":"http://x/z.zip","sha256":"ab"}}}"#,
        )
        .unwrap();
        assert!(shell_can_run(m.min_shell_abi, SHELL_ABI));
        // ...while an ABI-1 shell is offered no core that requires the token: it
        // keeps its bundled core until a full app update brings a new shell.
        assert!(!shell_can_run(m.min_shell_abi, 1));
        // Older cores (min_shell_abi 1) still run on either shell.
        assert!(shell_can_run(1, 1));
        assert!(shell_can_run(1, SHELL_ABI));
    }

    #[test]
    fn sha256_matches_known_vector() {
        // sha256("abc")
        assert_eq!(
            sha256_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn bad_signature_rejected() {
        assert!(!verify_signature(b"data", "not a real minisig"));
    }

    // minisign-verify's own prehashed test vector (key, signature over b"test").
    // `tauri signer sign` writes this .minisig text base64-encoded as the .sig.
    const TEST_PUBKEY: &str = "RWQf6LRCGA9i53mlYecO4IzT51TGPpvWucNSCh1CBM0QTaLn73Y7GFO3";
    const TEST_MINISIG: &str = "untrusted comment: signature from minisign secret key
RUQf6LRCGA9i559r3g7V1qNyJDApGip8MfqcadIgT9CuhV3EMhHoN1mGTkUidF/z7SrlQgXdy8ofjb7bNJJylDOocrCo8KLzZwo=
trusted comment: timestamp:1556193335\tfile:test
y/rUw2y8/hOUYjZU71eHp/Wo1KZ40fGy2VJEDl34XMJM+TX48Ss/17u3IvIfbVR1FkZZSNCisQbuQY+bHwhEBg==";
    // The same vector's legacy (non-prehashed) signature, which we refuse.
    const TEST_LEGACY_MINISIG: &str = "untrusted comment: signature from minisign secret key
RWQf6LRCGA9i59SLOFxz6NxvASXDJeRtuZykwQepbDEGt87ig1BNpWaVWuNrm73YiIiJbq71Wi+dP9eKL8OC351vwIasSSbXxwA=
trusted comment: timestamp:1555779966\tfile:test
QtKMXWyYcwdpZAlPF7tE2ENJkRd1ujvKjlj1m9RtHTBnZPa5WKU5uWRs5GoP5M/VqE81QFuMKI5k/SfNQUaOAA==";

    #[test]
    fn tauri_signer_signature_verifies() {
        use base64::Engine;
        let b64 = |s: &str| base64::engine::general_purpose::STANDARD.encode(s);
        let sig = b64(TEST_MINISIG);
        assert!(verify_signature_with(TEST_PUBKEY, b"test", &sig));
        // A .sig file read from disk may carry a trailing newline.
        assert!(verify_signature_with(TEST_PUBKEY, b"test", &format!("{sig}\n")));
        // Tampered data, the release key, or a legacy signature: all refused.
        assert!(!verify_signature_with(TEST_PUBKEY, b"Test", &sig));
        assert!(!verify_signature(b"test", &sig));
        assert!(!verify_signature_with(TEST_PUBKEY, b"test", &b64(TEST_LEGACY_MINISIG)));
    }

    // A real sidecar archive and its digest, both written by scripts/zip-sidecar.py
    // (regenerate with tests/fixtures/sidecar-update/make_fixtures.py). It holds
    // evi-server/evi-server and evi-server.exe, so it stages on every platform.
    const REAL_ZIP: &[u8] =
        include_bytes!("../tests/fixtures/sidecar-update/sidecar-windows-x86_64.zip");
    const REAL_SHA256: &str =
        include_str!("../tests/fixtures/sidecar-update/sidecar-windows-x86_64.sha256");
    const HOSTILE_ZIP: &[u8] = include_bytes!("../tests/fixtures/sidecar-update/hostile.zip");

    fn fresh_dir(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("evi-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn real_archive_hash_checks_extracts_and_stages() {
        let home = fresh_dir("e2e-real");
        // Our hex digest matches hashlib's for the published archive (the
        // manifest's sha256 comes from that .sha256 file); upper case is fine too.
        assert_eq!(sha256_hex(REAL_ZIP), REAL_SHA256.trim());
        let pinned = REAL_SHA256.trim().to_uppercase();
        assert!(stage_archive(&home, "1.0.99", REAL_ZIP, &pinned).is_some());

        let dir = root(&home).join("1.0.99").join("evi-server");
        let lines: String = (0..64).map(|i| format!("eVi sidecar fixture line {i:03}\n")).collect();
        assert_eq!(
            std::fs::read_to_string(dir.join("_internal").join("base_library.txt")).unwrap(),
            lines
        );
        assert_eq!(
            std::fs::read(dir.join("_internal").join("lib").join("data.bin")).unwrap(),
            (0..=255u8).collect::<Vec<u8>>()
        );
        assert!(!root(&home).join(".staging-1.0.99").exists());

        // Flip the pointer as check_and_stage does: launch now picks it up.
        std::fs::write(root(&home).join("active"), "1.0.99").unwrap();
        assert_eq!(staged_sidecar(&home, "1.0.53"), Some(dir.join(sidecar_bin())));

        let _ = std::fs::remove_dir_all(&home);
    }

    #[test]
    fn tampered_or_corrupt_archive_stages_nothing() {
        let home = fresh_dir("e2e-bad");
        // One flipped byte fails the pinned-hash check.
        let mut tampered = REAL_ZIP.to_vec();
        let mid = tampered.len() / 2;
        tampered[mid] ^= 0x01;
        assert!(stage_archive(&home, "1.0.99", &tampered, REAL_SHA256.trim()).is_none());
        assert!(!root(&home).join("1.0.99").exists());

        // A truncated download whose hash matches (a bad manifest) still fails
        // closed at extraction and leaves no staging dir behind.
        let truncated = &REAL_ZIP[..REAL_ZIP.len() / 2];
        assert!(stage_archive(&home, "1.0.99", truncated, &sha256_hex(truncated)).is_none());
        assert!(!root(&home).join("1.0.99").exists());
        assert!(!root(&home).join(".staging-1.0.99").exists());

        let _ = std::fs::remove_dir_all(&home);
    }

    #[test]
    fn hostile_archive_entries_stay_inside_the_staging_dir() {
        let base = fresh_dir("e2e-hostile");
        let dest = base.join("stage");
        std::fs::create_dir_all(&dest).unwrap();
        extract_zip(HOSTILE_ZIP, &dest).unwrap();

        // `../escape.txt` and `evi-server/../../escape2.txt` would land in `base`.
        assert!(!base.join("escape.txt").exists());
        assert!(!base.join("escape2.txt").exists());
        assert_eq!(
            std::fs::read_to_string(dest.join("evi-server").join("ok.txt")).unwrap(),
            "inside\n"
        );
        let bin = dest.join("evi-server").join("evi-server");
        assert!(bin.is_file());
        #[cfg(unix)]
        {
            // The unix mode from the archive survives, so the binary can launch.
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(&bin).unwrap().permissions().mode();
            assert_eq!(mode & 0o777, 0o755);
        }

        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn effective_current_prefers_newer_of_active_and_bundled() {
        // Stale active older than the bundle → bundle is current (guard rejects it).
        assert_eq!(effective_current(Some("1.0.42".into()), "1.0.46"), "1.0.46");
        // Active newer than bundle (sidecar channel ahead) → active is current.
        assert_eq!(effective_current(Some("1.0.48".into()), "1.0.46"), "1.0.48");
        // Equal → active.
        assert_eq!(effective_current(Some("1.0.46".into()), "1.0.46"), "1.0.46");
        // No active pointer → bundle.
        assert_eq!(effective_current(None, "1.0.46"), "1.0.46");
    }

    #[test]
    fn staged_shadow_guard_respects_bundled_version() {
        let base = std::env::temp_dir().join(format!("evi-stg-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        let sc = root(&base);
        let bindir = sc.join("1.0.42").join("evi-server");
        std::fs::create_dir_all(&bindir).unwrap();
        std::fs::write(bindir.join(sidecar_bin()), b"x").unwrap(); // fake binary
        std::fs::write(sc.join("active"), "1.0.42").unwrap();

        // A newer *bundled* core (fresh full-app update) must NOT be shadowed by
        // the stale staged 1.0.42.
        assert!(staged_sidecar(&base, "1.0.46").is_none());
        // Equal or older bundle → the staged core is still preferred.
        assert!(staged_sidecar(&base, "1.0.42").is_some());
        assert!(staged_sidecar(&base, "1.0.40").is_some());
        // No `active` pointer → nothing staged.
        std::fs::remove_file(sc.join("active")).unwrap();
        assert!(staged_sidecar(&base, "1.0.0").is_none());

        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn config_auto_update_default_and_override() {
        let base = std::env::temp_dir().join(format!("evi-cfg-{}", std::process::id()));
        let _ = std::fs::create_dir_all(&base);
        let cfg = base.join("config.toml");

        // No file → default on.
        let _ = std::fs::remove_file(&cfg);
        assert!(config_auto_update_enabled(&base));
        // Explicit false → off.
        std::fs::write(&cfg, "[desktop]\nsidecar_auto_update = false\n").unwrap();
        assert!(!config_auto_update_enabled(&base));
        // Explicit true → on.
        std::fs::write(&cfg, "[desktop]\nsidecar_auto_update = true\n").unwrap();
        assert!(config_auto_update_enabled(&base));
        // Unrelated config (section absent) → default on.
        std::fs::write(&cfg, "[llm]\nbackend = \"ollama\"\n").unwrap();
        assert!(config_auto_update_enabled(&base));

        let _ = std::fs::remove_dir_all(&base);
    }
}
