use std::io::Write;
#[cfg(test)]
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::OnceLock;

use super::{read_limited_reader, ClipboardCommand, ClipboardImage, LimitedRead, Signal};

#[path = "macos_process.rs"]
pub(crate) mod macos_process;
pub use macos_process::{
    foreground_process_group_id, foreground_process_group_id_for_tty_fd, process_cwd,
    session_processes,
};

const SERVER_NOFILE_LIMIT_TARGET: libc::rlim_t = 8192;

#[cfg(test)]
fn raw_command_argv(command: &str, flag: &str) -> Vec<std::ffi::OsString> {
    vec!["/bin/sh".into(), flag.into(), command.into()]
}

#[cfg(test)]
pub(crate) fn detached_custom_command_process_platform(command: &str) -> std::process::Command {
    let argv = raw_command_argv(command, "-lc");
    let mut command = std::process::Command::new(&argv[0]);
    command.args(&argv[1..]);
    command
}

#[cfg(test)]
pub(crate) fn pane_custom_command_pty_builder_platform(
    command: &str,
) -> portable_pty::CommandBuilder {
    portable_pty::CommandBuilder::from_argv(raw_command_argv(command, "-c"))
}

#[cfg(test)]
pub(crate) fn scrollback_editor_argv(path: &Path) -> std::io::Result<Vec<String>> {
    let quoted_path = shell_quote(&path.display().to_string());
    let command = format!(
        r#"scrollback_file={quoted_path}; eval "${{EDITOR:-vi}} \"\$scrollback_file\""; status=$?; rm -f "$scrollback_file"; exit $status"#
    );
    Ok(vec!["/bin/sh".to_string(), "-c".to_string(), command])
}

#[cfg(test)]
pub(crate) fn interactive_shell_command(argv: &[String], shell_name: &str) -> Option<String> {
    super::interactive_unix_shell_command(argv, shell_name, shell_quote)
}

#[cfg(test)]
fn shell_quote(value: &str) -> String {
    if !value.is_empty()
        && value.chars().all(|ch| {
            ch.is_ascii_alphanumeric()
                || matches!(
                    ch,
                    '@' | '%' | '_' | '+' | '=' | ':' | ',' | '.' | '/' | '-'
                )
        })
    {
        return value.to_string();
    }

    format!("'{}'", value.replace('\'', "'\\''"))
}

pub fn raise_server_nofile_limit() {
    match raise_nofile_limit(SERVER_NOFILE_LIMIT_TARGET) {
        Ok(None) => {}
        Ok(Some((previous, target))) => {
            tracing::info!(previous, target, "raised server file descriptor soft limit")
        }
        Err(err) => tracing::warn!(err = %err, "failed to raise server file descriptor limit"),
    }
}

fn raise_nofile_limit(
    target: libc::rlim_t,
) -> std::io::Result<Option<(libc::rlim_t, libc::rlim_t)>> {
    let mut limit = std::mem::MaybeUninit::<libc::rlimit>::uninit();
    if unsafe { libc::getrlimit(libc::RLIMIT_NOFILE, limit.as_mut_ptr()) } != 0 {
        return Err(std::io::Error::last_os_error());
    }

    let mut limit = unsafe { limit.assume_init() };
    let Some(target) = target_nofile_soft_limit(limit.rlim_cur, limit.rlim_max, target) else {
        return Ok(None);
    };

    let previous = limit.rlim_cur;
    limit.rlim_cur = target;
    if unsafe { libc::setrlimit(libc::RLIMIT_NOFILE, &limit) } != 0 {
        return Err(std::io::Error::last_os_error());
    }

    Ok(Some((previous, target)))
}

fn target_nofile_soft_limit(
    current: libc::rlim_t,
    hard: libc::rlim_t,
    target: libc::rlim_t,
) -> Option<libc::rlim_t> {
    let target = if hard == libc::RLIM_INFINITY {
        target
    } else {
        target.min(hard)
    };

    (current < target).then_some(target)
}

pub fn write_clipboard(bytes: &[u8]) -> bool {
    run_clipboard_command(
        &ClipboardCommand {
            program: "pbcopy",
            args: &[],
        },
        bytes,
    )
}

pub fn read_clipboard_text() -> Option<String> {
    const MAX_CLIPBOARD_TEXT_BYTES: usize = 1024 * 1024;

    let mut child = Command::new("pbpaste")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    let stdout = child.stdout.take()?;
    let read = match read_limited_reader(stdout, MAX_CLIPBOARD_TEXT_BYTES) {
        Ok(LimitedRead::Oversized) => {
            let _ = child.kill();
            let _ = child.wait();
            return None;
        }
        Ok(read) => read,
        Err(_) => {
            let _ = child.kill();
            let _ = child.wait();
            return None;
        }
    };
    let status = child.wait().ok()?;
    if !status.success() {
        return None;
    }
    match read {
        LimitedRead::Complete(bytes) => String::from_utf8(bytes).ok(),
        LimitedRead::Empty => None,
        LimitedRead::Oversized => unreachable!("oversized clipboard text is handled before wait"),
    }
}

pub fn open_url(url: &str) -> std::io::Result<()> {
    Command::new("open")
        .arg(url)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()?;
    Ok(())
}

pub fn read_clipboard_image() -> Option<ClipboardImage> {
    let path = std::env::temp_dir().join(format!(
        "gterm-clipboard-image-{}-{}.png",
        std::process::id(),
        unique_timestamp_nanos()
    ));
    let script = format!(
        "set png_data to (the clipboard as «class PNGf»)\nset fp to open for access POSIX file \"{}\" with write permission\nwrite png_data to fp\nclose access fp",
        path.display()
    );

    let status = Command::new("osascript")
        .arg("-e")
        .arg(script)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .ok()?;

    if !status.success() {
        let _ = std::fs::remove_file(&path);
        return None;
    }

    let bytes = match std::fs::File::open(&path).ok().and_then(|file| {
        read_limited_reader(file, crate::protocol::MAX_CLIPBOARD_IMAGE_PAYLOAD).ok()
    }) {
        Some(LimitedRead::Complete(bytes)) => bytes,
        Some(LimitedRead::Empty | LimitedRead::Oversized) | None => {
            let _ = std::fs::remove_file(&path);
            return None;
        }
    };
    let _ = std::fs::remove_file(&path);
    Some(ClipboardImage {
        bytes,
        extension: "png",
    })
}

fn unique_timestamp_nanos() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(0)
}

/// Show a native macOS notification.
///
/// Prefer `terminal-notifier` when it is installed because it can activate the
/// hosting terminal on click. Fall back to built-in AppleScript notifications
/// when it is not available.
pub fn show_desktop_notification(title: &str, body: Option<&str>) -> std::io::Result<bool> {
    show_desktop_notification_with_command(title, body, |program| Command::new(program))
}

fn show_desktop_notification_with_command(
    title: &str,
    body: Option<&str>,
    mut command: impl FnMut(&str) -> Command,
) -> std::io::Result<bool> {
    if show_terminal_notifier_notification(title, body, &mut command).unwrap_or(false) {
        return Ok(true);
    }

    show_osascript_notification(title, body, &mut command)
}

fn show_terminal_notifier_notification(
    title: &str,
    body: Option<&str>,
    command: &mut impl FnMut(&str) -> Command,
) -> std::io::Result<bool> {
    let activate_bundle_id = verified_terminal_bundle_identifier(command);
    show_terminal_notifier_notification_with_options(
        title,
        body,
        activate_bundle_id.as_deref(),
        command,
    )
}

fn show_terminal_notifier_notification_with_options(
    title: &str,
    body: Option<&str>,
    activate_bundle_id: Option<&str>,
    command: &mut impl FnMut(&str) -> Command,
) -> std::io::Result<bool> {
    let mut cmd = command("terminal-notifier");
    build_terminal_notifier_command(&mut cmd, title, body, activate_bundle_id);
    run_notification_command(cmd)
}

fn build_terminal_notifier_command(
    cmd: &mut Command,
    title: &str,
    body: Option<&str>,
    activate_bundle_id: Option<&str>,
) {
    cmd.arg("-title").arg(title);
    cmd.arg("-message").arg(body.unwrap_or_default());
    if let Some(bundle_id) = activate_bundle_id {
        cmd.arg("-activate").arg(bundle_id);
    }
}

fn show_osascript_notification(
    title: &str,
    body: Option<&str>,
    command: &mut impl FnMut(&str) -> Command,
) -> std::io::Result<bool> {
    let mut cmd = command("/usr/bin/osascript");
    cmd.arg("-e")
        .arg("on run argv")
        .arg("-e")
        .arg("display notification (item 2 of argv) with title (item 1 of argv)")
        .arg("-e")
        .arg("end run")
        .arg(title)
        .arg(body.unwrap_or_default());
    run_notification_command(cmd)
}

fn verified_terminal_bundle_identifier(
    command: &mut impl FnMut(&str) -> Command,
) -> Option<String> {
    static BUNDLE_ID: OnceLock<Option<String>> = OnceLock::new();
    BUNDLE_ID
        .get_or_init(|| {
            let bundle_id = detected_terminal_bundle_identifier()?;
            bundle_identifier_available(bundle_id, command).then(|| bundle_id.to_owned())
        })
        .clone()
}

fn bundle_identifier_available(bundle_id: &str, command: &mut impl FnMut(&str) -> Command) -> bool {
    let query = format!("kMDItemCFBundleIdentifier == '{bundle_id}'");
    let output = command("mdfind")
        .arg(query)
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output();

    match output {
        Ok(output) if output.status.success() => !output.stdout.is_empty(),
        _ => false,
    }
}

fn detected_terminal_bundle_identifier() -> Option<&'static str> {
    terminal_bundle_identifier_from_env(
        std::env::var("TERM_PROGRAM").ok().as_deref(),
        std::env::var("TERM").ok().as_deref(),
        std::env::var_os("KITTY_WINDOW_ID").is_some(),
        std::env::var_os("ALACRITTY_WINDOW_ID").is_some(),
    )
}

fn terminal_bundle_identifier_from_env(
    term_program: Option<&str>,
    term: Option<&str>,
    has_kitty_window_id: bool,
    has_alacritty_window_id: bool,
) -> Option<&'static str> {
    match term_program {
        Some("ghostty") => return Some("com.mitchellh.ghostty"),
        Some("iTerm.app") => return Some("com.googlecode.iterm2"),
        Some("WezTerm") => return Some("com.github.wez.wezterm"),
        Some("Apple_Terminal") => return Some("com.apple.Terminal"),
        _ => {}
    }

    if has_kitty_window_id || term == Some("xterm-kitty") {
        return Some("net.kovidgoyal.kitty");
    }
    if has_alacritty_window_id {
        return Some("org.alacritty");
    }

    None
}

fn run_notification_command(mut command: Command) -> std::io::Result<bool> {
    let status = match command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
    {
        Ok(status) => status,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => return Ok(false),
        Err(err) => return Err(err),
    };

    Ok(status.success())
}

fn run_clipboard_command(command: &ClipboardCommand, bytes: &[u8]) -> bool {
    let mut child = match Command::new(command.program)
        .args(command.args)
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
    {
        Ok(child) => child,
        Err(_) => return false,
    };

    let Some(mut stdin) = child.stdin.take() else {
        let _ = child.kill();
        let _ = child.wait();
        return false;
    };

    if stdin.write_all(bytes).is_err() {
        let _ = child.kill();
        let _ = child.wait();
        return false;
    }
    drop(stdin);

    child.wait().map(|status| status.success()).unwrap_or(false)
}

pub fn signal_processes(pids: &[u32], signal: Signal) {
    let sig = match signal {
        Signal::Hangup => libc::SIGHUP,
        Signal::Terminate => libc::SIGTERM,
        Signal::Kill => libc::SIGKILL,
    };

    for &pid in pids {
        if pid == 0 {
            continue;
        }
        unsafe {
            libc::kill(pid as libc::c_int, sig);
        }
    }
}

pub fn process_exists(pid: u32) -> bool {
    if pid == 0 {
        return false;
    }
    let result = unsafe { libc::kill(pid as libc::c_int, 0) };
    if result == 0 {
        true
    } else {
        std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
    }
}

#[cfg(test)]
#[path = "macos/tests.rs"]
mod tests;
