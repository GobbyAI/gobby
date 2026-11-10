//! Bearer probe for the gcode graphify rerun harness (`auth_probe` in validate_environment.py).
//!
//! Usage: managed_bearer_probe --home <gobby-home> --cache <grant-cache.json> \
//!        --daemon-url <url> --mode valid|tampered
//!
//! The isolated local key is read in-process by gobby_core. It is never an argument, never
//! printed and never copied. `tampered` flips one signature character in memory before the
//! request. Stdout is one JSON line with the outcome only: `{"mode", "verdict": "ok",
//! "config_revision"}`, or `{"mode", "verdict"}` carrying the daemon refusal's CLI code.

use std::path::PathBuf;
use std::time::Duration;

use anyhow::{Context, bail};

const TIMEOUT: Duration = Duration::from_secs(10);

fn main() -> anyhow::Result<()> {
    let mut home = None;
    let mut cache = None;
    let mut daemon_url = None;
    let mut mode = None;
    let mut args = std::env::args().skip(1);
    while let Some(flag) = args.next() {
        let value = args
            .next()
            .with_context(|| format!("{flag} needs a value"))?;
        match flag.as_str() {
            "--home" => home = Some(PathBuf::from(value)),
            "--cache" => cache = Some(PathBuf::from(value)),
            "--daemon-url" => daemon_url = Some(value),
            "--mode" => mode = Some(value),
            other => bail!("unknown flag {other}"),
        }
    }
    let home = home.context("--home is required")?;
    let cache = cache.context("--cache is required")?;
    let daemon_url = daemon_url.context("--daemon-url is required")?;
    let mode = mode.context("--mode is required")?;

    let mut grant = gobby_core::grant::load_grant_file(&cache)?;
    match mode.as_str() {
        "valid" => {}
        "tampered" => {
            let first = grant
                .signature
                .chars()
                .next()
                .context("grant has no signature")?;
            let flipped = if first == 'A' { 'B' } else { 'A' };
            grant
                .signature
                .replace_range(..first.len_utf8(), &flipped.to_string());
        }
        other => bail!("unknown mode {other}"),
    }
    let bearer = gobby_core::local_token::read_api_key_for(&home)?;
    let outcome = gobby_core::grant::fetch_runtime_config(
        &daemon_url,
        &grant,
        Some(bearer.as_str()),
        TIMEOUT,
    );
    let verdict = match outcome {
        Ok(settings) => serde_json::json!({
            "mode": mode,
            "verdict": "ok",
            "config_revision": settings.config_revision,
        }),
        Err(error) => serde_json::json!({"mode": mode, "verdict": error.cli_code()}),
    };
    println!("{verdict}");
    Ok(())
}
