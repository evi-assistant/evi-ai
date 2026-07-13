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
pub const SHELL_ABI: u32 = 1;

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
pub fn staged_sidecar(evi_home: &Path) -> Option<PathBuf> {
    let ver = active_version(evi_home)?;
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

/// Verify the manifest signature over `data` with our embedded public key.
/// `sig_b64` is the `.sig` produced by `tauri signer sign` — base64 of the raw
/// minisign `.minisig` file text (same key/format as the Tauri app updater).
fn verify_signature(data: &[u8], sig_b64: &str) -> bool {
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
    let pk = match PublicKey::from_base64(PUBKEY) {
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
fn http_get_bytes(url: &str) -> Option<Vec<u8>> {
    let resp = ureq::get(url).call().ok()?;
    let mut buf = Vec::new();
    resp.into_reader().take(512 * 1024 * 1024).read_to_end(&mut buf).ok()?;
    Some(buf)
}

fn http_get_string(url: &str) -> Option<String> {
    ureq::get(url).call().ok()?.into_string().ok()
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
                if let Some(mode) = entry.unix_mode() {
                    let _ = std::fs::set_permissions(&out, std::fs::Permissions::from_mode(mode));
                }
            }
        }
    }
    Ok(())
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

    if manifest.min_shell_abi > SHELL_ABI {
        // Needs a newer shell than this one — a full app update will bring it.
        return None;
    }
    // The version currently in effect: a staged one wins over the bundled one.
    let current = active_version(evi_home).unwrap_or_else(|| bundled_version.to_string());
    if !is_newer(&manifest.version, &current) {
        return None;
    }
    let entry = manifest.platforms.get(plat)?;

    let dest = root(evi_home).join(&manifest.version);
    // Already fully staged from a prior run (but active not yet flipped)? re-verify by presence.
    let bin = dest.join("evi-server").join(sidecar_bin());
    if !bin.is_file() {
        let zip_bytes = http_get_bytes(&entry.url)?;
        if sha256_hex(&zip_bytes) != entry.sha256.to_lowercase() {
            eprintln!("evi: sidecar zip sha256 mismatch — ignoring");
            return None;
        }
        // Extract into a temp dir first, then rename into place (atomic-ish).
        let tmp = root(evi_home).join(format!(".staging-{}", manifest.version));
        let _ = std::fs::remove_dir_all(&tmp);
        std::fs::create_dir_all(&tmp).ok()?;
        if extract_zip(&zip_bytes, &tmp).is_err() {
            let _ = std::fs::remove_dir_all(&tmp);
            return None;
        }
        let _ = std::fs::remove_dir_all(&dest);
        if std::fs::rename(&tmp, &dest).is_err() {
            let _ = std::fs::remove_dir_all(&tmp);
            return None;
        }
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
    text.parse::<toml::Value>()
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
/// sidecar version.
pub fn spawn_check(evi_home: PathBuf, bundled_version: String) {
    let env_off = std::env::var("EVI_SIDECAR_UPDATE")
        .map(|v| v == "0" || v.eq_ignore_ascii_case("false"))
        .unwrap_or(false);
    if env_off || !config_auto_update_enabled(&evi_home) {
        return;
    }
    std::thread::spawn(move || {
        if let Some(v) = check_and_stage(&evi_home, &bundled_version) {
            eprintln!("evi: staged sidecar {v} — active on next launch");
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
