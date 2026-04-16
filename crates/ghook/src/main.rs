//! ghook — sandbox-tolerant hook dispatcher.
//!
//! Three modes:
//!   `ghook --gobby-owned --cli=<c> --type=<t> [--critical] [--detach]`
//!   `ghook --diagnose    --cli=<c> --type=<t>`
//!   `ghook --version`
//!
//! Mode 1 enqueues an envelope to `~/.gobby/hooks/inbox/` and attempts a
//! POST to the daemon. On 2xx, the envelope is deleted; otherwise it
//! persists and the drain replays it. Exit codes:
//!   0 — success or non-critical failure (enqueued)
//!   2 — critical failure (enqueued)
//!
//! Mode 2 prints a JSON diagnostic, no network, no envelope write.
//!
//! Mode 3 prints the ghook version and writes
//! `~/.gobby/bin/.ghook-compatibility` with `{schema_version, ghook_version}`.

use anyhow::Result;
use clap::Parser;
use serde_json::Value;
use std::collections::BTreeMap;
use std::io::Read;
use std::path::PathBuf;
use std::process::ExitCode;

mod cli_config;
mod detach;
mod diagnose;
mod envelope;
mod terminal_context;
mod transport;

use cli_config::CliConfig;
use envelope::Envelope;

#[derive(Parser, Debug)]
#[command(
    name = "ghook",
    about = "Gobby sandbox-tolerant hook dispatcher",
    disable_version_flag = true
)]
struct Args {
    /// Normal hook-invocation mode. Required for enqueue/POST.
    #[arg(long)]
    gobby_owned: bool,

    /// Print diagnostic JSON for the given cli/type, then exit.
    #[arg(long)]
    diagnose: bool,

    /// Print version and write ~/.gobby/bin/.ghook-compatibility stamp.
    #[arg(long)]
    version: bool,

    /// Host CLI name (claude, codex, gemini, qwen).
    #[arg(long)]
    cli: Option<String>,

    /// Hook type (e.g. session-start, SessionStart, PreToolUse).
    #[arg(long = "type")]
    hook_type: Option<String>,

    /// Mark this hook as critical — failure exits 2 (signals host CLI).
    #[arg(long)]
    critical: bool,

    /// Detach from the parent's session/process group before the POST.
    #[arg(long)]
    detach: bool,
}

fn main() -> ExitCode {
    let args = Args::parse();

    if args.version {
        return match write_compatibility_stamp() {
            Ok(()) => ExitCode::SUCCESS,
            Err(e) => {
                // Still print the version; stamp-write failure is non-fatal.
                println!("ghook {}", diagnose::GHOOK_VERSION);
                eprintln!("note: could not write compatibility stamp: {e}");
                ExitCode::SUCCESS
            }
        };
    }
    if args.diagnose {
        return run_diagnose(&args);
    }
    if args.gobby_owned {
        return run_gobby_owned(&args);
    }

    eprintln!("ghook: no mode specified; use one of --gobby-owned, --diagnose, or --version");
    ExitCode::from(2)
}

fn run_diagnose(args: &Args) -> ExitCode {
    let (Some(cli), Some(hook_type)) = (args.cli.as_deref(), args.hook_type.as_deref()) else {
        eprintln!("--diagnose requires --cli and --type");
        return ExitCode::from(2);
    };
    let out = diagnose::diagnose(cli, hook_type);
    match serde_json::to_string_pretty(&out) {
        Ok(s) => {
            println!("{s}");
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("diagnose serialization failed: {e}");
            ExitCode::from(2)
        }
    }
}

fn run_gobby_owned(args: &Args) -> ExitCode {
    let (Some(cli), Some(hook_type)) = (args.cli.as_deref(), args.hook_type.as_deref()) else {
        eprintln!("--gobby-owned requires --cli and --type");
        return if args.critical {
            ExitCode::from(2)
        } else {
            ExitCode::SUCCESS
        };
    };

    // IMPORTANT: walk up for project context BEFORE any detach.
    // Sandbox FS-read denials or a detached process's cwd semantics on
    // macOS would otherwise surprise us (plan :76).
    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    let project_root = gobby_core::project::find_project_root(&cwd);
    let project_id = project_root
        .as_ref()
        .and_then(|r| gobby_core::project::read_project_id(r).ok());

    // Read stdin before detach too — detach closes the controlling TTY but
    // stdin pipes from the host CLI should still be intact; read now to
    // avoid late-read surprises if the host closes the pipe on exit.
    let mut stdin_raw = Vec::with_capacity(4096);
    let read_ok = std::io::stdin().read_to_end(&mut stdin_raw).is_ok();

    // Parse. On malformed stdin: quarantine + exit (0 or 2).
    let parsed: Result<Value, serde_json::Error> = if read_ok && !stdin_raw.is_empty() {
        serde_json::from_slice(&stdin_raw)
    } else {
        // Empty stdin is treated as an empty object — dispatcher accepts
        // missing input_data and continues (`:677` with empty raw parses
        // as null, which fails there; here we normalize to {} for the
        // non-critical case).
        Ok(Value::Object(Default::default()))
    };

    let mut input_data = match parsed {
        Ok(v) => v,
        Err(e) => {
            if let Err(werr) =
                transport::quarantine_malformed(&stdin_raw, &e.to_string(), args.critical)
            {
                eprintln!("quarantine write failed: {werr}");
            }
            return if args.critical {
                ExitCode::from(2)
            } else {
                ExitCode::SUCCESS
            };
        }
    };

    // Recognize the CLI; unknown CLI is tolerated (use a conservative
    // fallback) so hook scripts written for future CLIs don't break.
    let cfg = CliConfig::for_cli(cli);

    // Terminal-context enrichment, gated by per-CLI set.
    if let Some(c) = &cfg
        && c.wants_terminal_context(hook_type)
    {
        terminal_context::inject(&mut input_data);
    }

    // Headers: omit on missing (never empty string).
    let mut headers: BTreeMap<String, String> = BTreeMap::new();
    if let Some(pid) = project_id {
        headers.insert("X-Gobby-Project-Id".into(), pid);
    }
    if let Some(sid) = input_data.get("session_id").and_then(|v| v.as_str())
        && !sid.is_empty()
    {
        headers.insert("X-Gobby-Session-Id".into(), sid.to_string());
    }

    let source = cfg
        .as_ref()
        .map(|c| c.source.to_string())
        .unwrap_or_else(|| cli.to_string());

    let env = Envelope::new(
        args.critical,
        hook_type.to_string(),
        input_data,
        source,
        headers,
    );

    // Enqueue first (atomic write to ~/.gobby/hooks/inbox/).
    let inbox = match transport::inbox_dir() {
        Ok(d) => d,
        Err(e) => {
            eprintln!("resolve inbox dir: {e}");
            return if args.critical {
                ExitCode::from(2)
            } else {
                ExitCode::SUCCESS
            };
        }
    };
    let enqueued_path = match transport::enqueue_to(&env, &inbox) {
        Ok(p) => p,
        Err(e) => {
            eprintln!("enqueue failed: {e}");
            return if args.critical {
                ExitCode::from(2)
            } else {
                ExitCode::SUCCESS
            };
        }
    };

    // Detach *after* project walk-up and enqueue — the file on disk is
    // now the source of truth even if we die mid-POST.
    if args.detach {
        detach::detach();
    }

    // Best-effort POST. Enqueue file is deleted on 2xx; otherwise kept.
    let daemon_url = gobby_core::daemon_url::daemon_url();
    let outcome = transport::post_and_cleanup(&env, &enqueued_path, &daemon_url);

    match outcome {
        transport::DeliveryOutcome::Delivered => ExitCode::SUCCESS,
        transport::DeliveryOutcome::Enqueued => {
            if args.critical {
                ExitCode::from(2)
            } else {
                ExitCode::SUCCESS
            }
        }
    }
}

fn write_compatibility_stamp() -> Result<()> {
    let home = dirs::home_dir().ok_or_else(|| anyhow::anyhow!("no home directory"))?;
    let bin_dir = home.join(".gobby").join("bin");
    std::fs::create_dir_all(&bin_dir)?;
    let stamp_path = bin_dir.join(".ghook-compatibility");
    let stamp = serde_json::json!({
        "schema_version": envelope::SCHEMA_VERSION,
        "ghook_version": diagnose::GHOOK_VERSION,
    });
    let bytes = serde_json::to_vec_pretty(&stamp)?;
    transport::atomic_write(&stamp_path, &bytes)?;
    println!("ghook {}", diagnose::GHOOK_VERSION);
    Ok(())
}
