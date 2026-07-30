//! Claude Code statusline display adapter.
//!
//! This is intentionally separate from the normal enqueue-first hook path.
//! Claude reads statusline stdout directly on every tick, so the handler must
//! preserve downstream stdout bytes exactly.

use std::ffi::OsStr;
use std::io::{Read, Write};
use std::process::{Command, ExitCode, Stdio};
use std::thread;
use std::time::{Duration, Instant};

#[cfg(not(target_os = "windows"))]
use std::os::unix::process::CommandExt;

const DOWNSTREAM_TIMEOUT: Duration = Duration::from_secs(5);

pub(crate) fn is_statusline_hook(cli: &str, hook_type: &str) -> bool {
    cli.eq_ignore_ascii_case("claude") && hook_type == "statusline"
}

pub(crate) fn handle(stdin_raw: &[u8]) -> ExitCode {
    let downstream = std::env::var_os("GOBBY_STATUSLINE_DOWNSTREAM");
    let downstream = downstream.as_deref().filter(|command| !command.is_empty());
    let mut stdout = std::io::stdout().lock();
    handle_with(stdin_raw, downstream, &mut stdout)
}

fn handle_with(stdin_raw: &[u8], downstream: Option<&OsStr>, stdout: &mut impl Write) -> ExitCode {
    if let Some(command) = downstream
        && let Some(bytes) = forward_downstream(command, stdin_raw)
    {
        let _ = stdout.write_all(&bytes);
        let _ = stdout.flush();
    }

    ExitCode::SUCCESS
}

fn forward_downstream(command: &OsStr, stdin_raw: &[u8]) -> Option<Vec<u8>> {
    let mut child = downstream_shell_command(command)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;

    let mut stdout_pipe = child.stdout.take()?;
    let stdout_reader = thread::spawn(move || {
        let mut stdout = Vec::new();
        let _ = stdout_pipe.read_to_end(&mut stdout);
        stdout
    });

    let stdin_writer = child.stdin.take().map(|mut stdin| {
        // Python's Popen.communicate(input=...) tolerates a downstream that
        // exits without reading stdin (e.g. `printf`). Still collect stdout.
        let stdin_raw = stdin_raw.to_vec();
        thread::spawn(move || {
            let _ = stdin.write_all(&stdin_raw);
        })
    });

    let started = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(_status)) => {
                if let Some(writer) = stdin_writer {
                    let _ = writer.join();
                }
                let stdout = stdout_reader.join().ok()?;
                return (!stdout.is_empty()).then_some(stdout);
            }
            Ok(None) if started.elapsed() < DOWNSTREAM_TIMEOUT => {
                thread::sleep(Duration::from_millis(10));
            }
            Ok(None) | Err(_) => {
                terminate_downstream(&mut child);
                if let Some(writer) = stdin_writer {
                    let _ = writer.join();
                }
                let _ = stdout_reader.join();
                return None;
            }
        }
    }
}

fn terminate_downstream(child: &mut std::process::Child) {
    terminate_downstream_group(child);
    let _ = child.kill();
    let _ = child.wait();
}

#[cfg(not(target_os = "windows"))]
fn terminate_downstream_group(child: &std::process::Child) {
    // SAFETY: downstream_shell_command puts the shell in a fresh process group
    // whose pgid equals the child pid, so this only targets that downstream tree.
    unsafe {
        libc::killpg(child.id() as libc::pid_t, libc::SIGKILL);
    }
}

#[cfg(target_os = "windows")]
fn terminate_downstream_group(_child: &std::process::Child) {}

#[cfg(not(target_os = "windows"))]
fn downstream_shell_command(command: &OsStr) -> Command {
    let mut shell = Command::new("sh");
    shell.arg("-c").arg(command);
    shell.process_group(0);
    shell
}

#[cfg(target_os = "windows")]
fn downstream_shell_command(command: &OsStr) -> Command {
    let mut shell = Command::new("cmd");
    shell.arg("/C").arg(command);
    shell
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recognizes_only_claude_statusline_hook() {
        assert!(is_statusline_hook("claude", "statusline"));
        assert!(is_statusline_hook("CLAUDE", "statusline"));
        assert!(!is_statusline_hook("claude", "PreToolUse"));
        assert!(!is_statusline_hook("codex", "statusline"));
    }

    #[test]
    fn downstream_stdin_passthrough_preserves_bytes() {
        if cfg!(target_os = "windows") {
            return;
        }

        let stdin = b"not json\n\x00statusline bytes";
        let mut stdout = Vec::new();
        let exit = handle_with(stdin, Some(OsStr::new("cat")), &mut stdout);

        assert_eq!(exit, ExitCode::SUCCESS);
        assert_eq!(stdout, stdin);
    }

    #[test]
    fn downstream_stdout_passthrough_preserves_bytes() {
        let mut stdout = Vec::new();
        let exit = handle_with(
            br#"{"session_id":"sess-123"}"#,
            Some(OsStr::new("printf 'status ok'")),
            &mut stdout,
        );

        assert_eq!(exit, ExitCode::SUCCESS);
        assert_eq!(stdout, b"status ok");
    }

    #[test]
    fn downstream_large_stdout_returns_full_output_quickly() {
        if cfg!(target_os = "windows") {
            return;
        }

        let started = Instant::now();
        let stdout = forward_downstream(
            OsStr::new("yes x | head -c 204800"),
            br#"{"session_id":"sess-123"}"#,
        )
        .expect("large downstream stdout should be captured");

        assert_eq!(stdout.len(), 204_800);
        assert!(
            started.elapsed() < Duration::from_secs(2),
            "large stdout should not wait for the downstream timeout"
        );
    }

    #[test]
    fn downstream_timeout_returns_before_six_seconds() {
        if cfg!(target_os = "windows") {
            return;
        }

        let stdin = format!(
            r#"{{"session_id":"sess-123","transcript_path":"{}"}}"#,
            "x".repeat(200 * 1024)
        );
        let started = Instant::now();
        let mut stdout = Vec::new();
        let exit = handle_with(stdin.as_bytes(), Some(OsStr::new("sleep 10")), &mut stdout);

        assert_eq!(exit, ExitCode::SUCCESS);
        assert!(stdout.is_empty());
        assert!(
            started.elapsed() < Duration::from_secs(6),
            "downstream timeout should fire before CI hangs"
        );
    }

    #[test]
    fn downstream_timeout_kills_pipeline_survivors_holding_stdout() {
        if cfg!(target_os = "windows") {
            return;
        }

        // The background sleep inherits the stdout write-end; only the
        // process-group kill releases the reader thread. Killing just the
        // direct shell child would leave this test blocked ~30s.
        let started = Instant::now();
        let mut stdout = Vec::new();
        let exit = handle_with(
            br#"{"session_id":"sess-123","transcript_path":"/tmp/t.jsonl"}"#,
            Some(OsStr::new("sleep 30 & sleep 30")),
            &mut stdout,
        );

        assert_eq!(exit, ExitCode::SUCCESS);
        assert!(stdout.is_empty());
        assert!(
            started.elapsed() < Duration::from_secs(7),
            "group kill should release the stdout reader despite survivors"
        );
    }
}
