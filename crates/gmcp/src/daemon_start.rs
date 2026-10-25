//! Starts a stopped local daemon once, at launch, through the recorded `gobby start` command.

use std::time::Duration;

/// How long the recorded start command may run; above `gobby start`'s own health and readiness waits.
pub const START_WAIT: Duration = Duration::from_secs(600);
/// How much of the start command's output a failure log keeps.
pub const OUTPUT_TAIL_CHARS: usize = 2_000;

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
    let _ = (argv, wait, tail_chars);
    todo!("run the recorded start command")
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
