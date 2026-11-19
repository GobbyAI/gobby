//! The process subscriber, run in a child process so it can be the global one.

use std::fmt;
use std::process::Command;
use std::sync::mpsc;
use std::time::Duration;

use super::*;

/// Set in the child process of the re-entrance test, to the logs directory.
const REENTRANT_CHILD: &str = "GOBBY_LOGGING_TEST_REENTRANT_CHILD";

/// A field whose formatting logs its own `gobby.*` record.
struct LogsWhenFormatted;

impl fmt::Debug for LogsWhenFormatted {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        tracing::warn!(target: "gobby.daemon", "record from inside formatting");
        formatter.write_str("formatted field")
    }
}

#[test]
fn a_field_that_logs_while_formatted_does_not_block_its_record() {
    if let Some(logs) = std::env::var_os(REENTRANT_CHILD) {
        // Child: the routed layer is the process's global subscriber, as in gdaemon.
        activate(settings_in(Path::new(&logs))).expect("routed logging activates");
        let (done, finished) = mpsc::channel();
        std::thread::spawn(move || {
            tracing::info!(target: "gobby.daemon", field = ?LogsWhenFormatted, "outer record");
            let _ = done.send(());
        });
        if finished.recv_timeout(Duration::from_secs(20)).is_err() {
            raw_stderr("the outer record did not finish: routing blocked on itself");
            std::process::exit(3);
        }
        return;
    }

    let logs = tempfile::tempdir().expect("temporary logs directory");
    let output = Command::new(std::env::current_exe().expect("test binary path"))
        .args([
            "--exact",
            "logging::tests::sink::a_field_that_logs_while_formatted_does_not_block_its_record",
            "--nocapture",
        ])
        .env(REENTRANT_CHILD, logs.path())
        .output()
        .expect("child test runs");

    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(output.status.success(), "{stderr}");
    let daemon = read(logs.path(), "daemon.log");
    assert!(daemon.contains("record from inside formatting"), "{daemon}");
    assert!(
        daemon.contains("outer record | field=formatted field"),
        "{daemon}"
    );
}

/// Set in the child process of the closed-stderr test.
#[cfg(unix)]
const CLOSED_STDERR_CHILD: &str = "GOBBY_LOGGING_TEST_CLOSED_STDERR_CHILD";

/// Printed by the closed-stderr child after its stderr write returned.
#[cfg(unix)]
const SURVIVED: &str = "raw stderr returned";

#[cfg(unix)]
#[test]
fn raw_stderr_returns_when_stderr_is_a_broken_pipe() {
    use std::io::{BufRead as _, Write as _};
    use std::process::Stdio;

    if std::env::var_os(CLOSED_STDERR_CHILD).is_some() {
        // Child: its stderr's reader is gone before this write.
        let mut line = String::new();
        std::io::stdin()
            .lock()
            .read_line(&mut line)
            .expect("the parent says go");
        raw_stderr("a line nobody reads");
        println!("{SURVIVED}");
        return;
    }

    let mut child = Command::new(std::env::current_exe().expect("test binary path"))
        .args([
            "--exact",
            "logging::tests::sink::raw_stderr_returns_when_stderr_is_a_broken_pipe",
            "--nocapture",
        ])
        .env(CLOSED_STDERR_CHILD, "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("child test runs");
    drop(child.stderr.take());
    let mut stdin = child.stdin.take().expect("child stdin");
    stdin.write_all(b"go\n").expect("the child reads stdin");
    drop(stdin);
    let output = child.wait_with_output().expect("child test finishes");

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(output.status.success(), "{:?}: {stdout}", output.status);
    assert!(stdout.contains(SURVIVED), "{stdout}");
}
