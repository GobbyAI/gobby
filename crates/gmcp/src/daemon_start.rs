//! Starts a stopped local daemon once, at launch, through the recorded `gobby start` command.

use std::io::{Read, Seek, SeekFrom};
use std::process::Stdio;
use std::sync::Arc;
use std::time::Duration;

use anyhow::Context;
use tokio::process::Command;

use crate::daemon::{Dialer, HealthProbe};

/// How long the recorded start command may run; above `gobby start`'s own health and readiness waits.
pub const START_WAIT: Duration = Duration::from_secs(600);
/// How much of the start command's output a failure log keeps.
pub const OUTPUT_TAIL_CHARS: usize = 2_000;
const HEALTH_ATTEMPTS: usize = 30;

/// Runs alongside stdio; dropping this task leaves a spawned start command alive.
pub async fn run_startup(dialer: Arc<Dialer>) {
    if let Err(error) = startup(dialer).await {
        eprintln!("gmcp: daemon startup failed: {error:#}");
    }
}

async fn probe(dialer: &Arc<Dialer>) -> anyhow::Result<HealthProbe> {
    let dialer = Arc::clone(dialer);
    tokio::task::spawn_blocking(move || dialer.probe_health()).await?
}

async fn startup(dialer: Arc<Dialer>) -> anyhow::Result<()> {
    let target = url::Url::parse(&gobby_core::daemon_url::daemon_url()?)?;
    let local = matches!(target.host_str(), Some("localhost" | "127.0.0.1" | "[::1]"));
    let health = probe(&dialer).await?;
    if !local {
        if health != HealthProbe::Healthy {
            eprintln!("gmcp: error: remote daemon health probe failed");
        }
        return Ok(());
    }
    match health {
        HealthProbe::Healthy => return Ok(()),
        HealthProbe::Answered(status) => {
            eprintln!("gmcp: warning: daemon health answered HTTP {status}");
            return Ok(());
        }
        HealthProbe::Unreachable => {}
    }
    if std::env::var_os("GOBBY_AGENT_RUN_ID").is_some_and(|value| !value.is_empty()) {
        eprintln!("gmcp: error: refusing to start a daemon from an agent process");
        return Ok(());
    }
    let record = gobby_core::gobby_home()?.join("gmcp-daemon-start.json");
    let bytes = match std::fs::read(record) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            eprintln!("gmcp: no start command recorded; run `gobby start`");
            return Ok(());
        }
        Err(error) => return Err(error).context("read daemon start record"),
    };
    let argv: Vec<String> = serde_json::from_slice(&bytes)
        .context("daemon start record must be a non-empty JSON array of strings")?;
    anyhow::ensure!(
        !argv.is_empty() && !argv[0].is_empty(),
        "daemon start record has no command"
    );
    match run_start_command(&argv, START_WAIT, OUTPUT_TAIL_CHARS).await? {
        StartOutcome::Exited { code: Some(0), .. } => {}
        StartOutcome::Exited { code, tail } => {
            eprintln!("gmcp: error: daemon start exited with code {code:?}: {tail}");
            return Ok(());
        }
        StartOutcome::TimedOut { tail } => {
            eprintln!(
                "gmcp: error: daemon start timed out after {}s: {tail}",
                START_WAIT.as_secs()
            );
            return Ok(());
        }
    }
    for attempt in 0..HEALTH_ATTEMPTS {
        if probe(&dialer).await? == HealthProbe::Healthy {
            eprintln!("gmcp: daemon healthy");
            return Ok(());
        }
        if attempt + 1 < HEALTH_ATTEMPTS {
            tokio::time::sleep(Duration::from_secs(1)).await;
        }
    }
    eprintln!("gmcp: error: daemon health failed after {HEALTH_ATTEMPTS} attempts");
    Ok(())
}

/// What one run of the recorded start command came to.
#[derive(Debug, PartialEq, Eq)]
pub enum StartOutcome {
    /// The command exited; `code` is `None` when a signal ended it.
    Exited { code: Option<i32>, tail: String },
    /// The command outlived its wait and was terminated.
    TimedOut { tail: String },
}

/// Run `argv` with stdin null and both output streams on one file, for at most `wait`.
pub async fn run_start_command(
    argv: &[String],
    wait: Duration,
    tail_chars: usize,
) -> anyhow::Result<StartOutcome> {
    let (program, args) = argv
        .split_first()
        .context("daemon start command is empty")?;
    let mut output = tempfile::tempfile().context("create daemon start output file")?;
    let mut child = Command::new(program)
        .args(args)
        .stdin(Stdio::null())
        .stdout(output.try_clone()?)
        .stderr(output.try_clone()?)
        .kill_on_drop(false)
        .spawn()
        .context("spawn recorded daemon start command")?;
    let code = match tokio::time::timeout(wait, child.wait()).await {
        Ok(status) => Some(status?.code()),
        Err(_) => {
            if let Some(pid) = child.id() {
                let term = Command::new("/bin/kill")
                    .args(["-TERM", &pid.to_string()])
                    .stdin(Stdio::null())
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .status()
                    .await;
                if term.is_err() {
                    child
                        .kill()
                        .await
                        .context("kill daemon start after TERM failed")?;
                } else {
                    match tokio::time::timeout(Duration::from_secs(5), child.wait()).await {
                        Ok(status) => {
                            status.context("wait for terminated daemon start")?;
                        }
                        Err(_) => child.kill().await.context("kill timed out daemon start")?,
                    }
                }
            }
            None
        }
    };
    // At most four UTF-8 bytes per character; seek so arbitrarily noisy children stay bounded.
    let bytes = u64::try_from(tail_chars.saturating_mul(4))?;
    let start = output.metadata()?.len().saturating_sub(bytes);
    output.seek(SeekFrom::Start(start))?;
    let mut tail = Vec::new();
    output.take(bytes).read_to_end(&mut tail)?;
    let tail: String = String::from_utf8_lossy(&tail)
        .chars()
        .rev()
        .take(tail_chars)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect();
    Ok(match code {
        Some(code) => StartOutcome::Exited { code, tail },
        None => StartOutcome::TimedOut { tail },
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;
    use std::time::Instant;

    fn script(dir: &Path, body: &str) -> Vec<String> {
        let path = dir.join("start.sh");
        std::fs::write(&path, body).unwrap();
        vec!["/bin/sh".into(), path.to_string_lossy().into_owned()]
    }

    #[tokio::test]
    async fn failed_start_logs_tail_and_timeout_terminates() {
        assert_eq!(START_WAIT, Duration::from_secs(600));
        assert_eq!(OUTPUT_TAIL_CHARS, 2_000);
        let dir = tempfile::tempdir().unwrap();

        // 600 characters on stdout, then 2,000 on stderr: the tail is exactly the stderr part.
        let failing = script(
            dir.path(),
            "head -c 600 /dev/zero | tr '\\0' x\nhead -c 2000 /dev/zero | tr '\\0' y >&2\nexit 3\n",
        );
        let outcome = run_start_command(&failing, Duration::from_secs(10), OUTPUT_TAIL_CHARS)
            .await
            .unwrap();
        assert_eq!(
            outcome,
            StartOutcome::Exited {
                code: Some(3),
                tail: "y".repeat(2_000),
            }
        );

        // A command that ignores SIGTERM is killed 5 s after its wait runs out.
        let term = dir.path().join("term");
        let beats = dir.path().join("beats");
        let stubborn = script(
            dir.path(),
            &format!(
                "trap 'echo term >> {term}' TERM\necho started\nwhile :; do echo beat >> {beats}; sleep 0.1; done\n",
                term = term.display(),
                beats = beats.display(),
            ),
        );
        let wait = Duration::from_millis(500);
        let started = Instant::now();
        let outcome = run_start_command(&stubborn, wait, OUTPUT_TAIL_CHARS)
            .await
            .unwrap();
        let elapsed = started.elapsed();
        assert_eq!(
            outcome,
            StartOutcome::TimedOut {
                tail: "started\n".into(),
            }
        );
        assert_eq!(std::fs::read_to_string(&term).unwrap(), "term\n");
        assert!(
            elapsed >= wait + Duration::from_millis(4_800),
            "killed after {elapsed:?}, before the 5 s grace"
        );
        assert!(
            elapsed < wait + Duration::from_secs(8),
            "not killed within the grace: {elapsed:?}"
        );
        assert_unchanged_for(&beats, Duration::from_millis(400));
    }

    /// Watch `path` for `window`: a killed command's heartbeat file stops growing.
    fn assert_unchanged_for(path: &Path, window: Duration) {
        let length = std::fs::metadata(path).unwrap().len();
        let deadline = Instant::now() + window;
        while Instant::now() < deadline {
            assert_eq!(
                std::fs::metadata(path).unwrap().len(),
                length,
                "the command kept running after the kill"
            );
            std::thread::sleep(Duration::from_millis(20));
        }
    }
}
