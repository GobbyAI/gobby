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
        activate(settings_in(Path::new(&logs)), None).expect("routed logging activates");
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

/// Set in the child process of the rotation-owner test, to the logs directory.
const NO_BACKEND_CHILD: &str = "GOBBY_LOGGING_TEST_NO_BACKEND_CHILD";

#[test]
fn activation_without_a_backend_command_rotates_its_own_files() {
    if let Some(logs) = std::env::var_os(NO_BACKEND_CHILD) {
        // Child: node mode, so no backend command and no worker to rotate the files.
        let settings = LogSettings {
            max_size_mb: 1,
            backup_count: 2,
            ..settings_in(Path::new(&logs))
        };
        activate(settings, None).expect("routed logging activates");
        tracing::info!(target: "gobby.daemon", "{}", large_message('a'));
        tracing::info!(target: "gobby.daemon", "{}", large_message('b'));
        return;
    }

    let logs = tempfile::tempdir().expect("temporary logs directory");
    let output = Command::new(std::env::current_exe().expect("test binary path"))
        .args([
            "--exact",
            "logging::tests::sink::activation_without_a_backend_command_rotates_its_own_files",
            "--nocapture",
        ])
        .env(NO_BACKEND_CHILD, logs.path())
        .output()
        .expect("child test runs");

    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(output.status.success(), "{stderr}");
    assert!(
        exists(logs.path(), "daemon.log.1"),
        "with no backend command gdaemon owns rotation, so the second large record \
         rotated daemon.log; found {:?}",
        files_under(logs.path())
    );
}

/// Set in the child process of the setting-diagnostic tests, to the logs directory.
const DIAGNOSTIC_CHILD: &str = "GOBBY_LOGGING_TEST_DIAGNOSTIC_CHILD";

/// Child: activates routing with `settings`, applies one epoch of `values` through the
/// process's applier, and returns once the applier has run.
fn apply_one_epoch_as_the_process(settings: LogSettings, values: serde_json::Value) {
    let serde_json::Value::Object(active_values) = values else {
        panic!("active values are an object");
    };
    activate(settings, None).expect("routed logging activates");
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("child runtime");
    runtime.block_on(async {
        let (_epochs, rx) = tokio::sync::watch::channel(gobby_core::family::ConfigEpoch {
            revision: 2,
            active_values: std::sync::Arc::new(active_values),
        });
        let applier = crate::logging::epoch::spawn_applier(rx);
        // The applier applies the current epoch on its first poll.
        for _ in 0..16 {
            tokio::task::yield_now().await;
        }
        applier.abort();
    });
}

/// Runs `test` in a child process with its logs under a new directory, and returns the
/// child's stderr once it has succeeded.
fn child_stderr(test: &str) -> String {
    let logs = tempfile::tempdir().expect("temporary logs directory");
    let output = Command::new(std::env::current_exe().expect("test binary path"))
        .args(["--exact", test, "--nocapture"])
        .env(DIAGNOSTIC_CHILD, logs.path())
        .env_remove("GOBBY_LOGGING_DIR")
        .output()
        .expect("child test runs");
    let stderr = String::from_utf8_lossy(&output.stderr).into_owned();
    assert!(output.status.success(), "{:?}: {stderr}", output.status);
    stderr
}

#[test]
fn an_invalid_setting_is_reported_on_stderr_at_an_error_floor() {
    if let Some(logs) = std::env::var_os(DIAGNOSTIC_CHILD) {
        let settings = LogSettings {
            level: Level::Error,
            ..settings_in(Path::new(&logs))
        };
        apply_one_epoch_as_the_process(settings, serde_json::json!({"logging.format": "yaml"}));
        return;
    }

    let stderr = child_stderr(
        "logging::tests::sink::an_invalid_setting_is_reported_on_stderr_at_an_error_floor",
    );
    assert!(
        stderr.contains("logging.format has an invalid value"),
        "the rejected logging.format was not reported on stderr: {stderr:?}"
    );
}

#[test]
fn an_invalid_setting_is_reported_when_its_epoch_raises_the_floor_to_error() {
    if let Some(logs) = std::env::var_os(DIAGNOSTIC_CHILD) {
        apply_one_epoch_as_the_process(
            settings_in(Path::new(&logs)),
            serde_json::json!({"logging.level": "error", "logging.backup_count": 0}),
        );
        return;
    }

    let stderr = child_stderr(
        "logging::tests::sink::an_invalid_setting_is_reported_when_its_epoch_raises_the_floor_to_error",
    );
    assert!(
        stderr.contains("logging.backup_count has an invalid value"),
        "the rejected logging.backup_count was not reported on stderr: {stderr:?}"
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
