//! Unix PTY spawn with a commit barrier (fifo, then exec).

#![cfg(unix)]

use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use crate::pane::{PaneLaunchEnv, PaneRuntime};
use crate::terminal_theme::TerminalTheme;

pub struct PreparedChild {
    pub runtime: PaneRuntime,
    pub pid: u32,
    pub pgid: i32,
    pub start_time: f64,
    gate: PathBuf,
}

impl PreparedChild {
    pub fn commit(&mut self) -> io::Result<()> {
        let mut file = OpenOptions::new()
            .write(true)
            .custom_flags(libc::O_NONBLOCK)
            .open(&self.gate)?;
        let _ = file.write_all(&[b'\n']);
        Ok(())
    }
}

impl Drop for PreparedChild {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.gate);
    }
}

pub fn spawn_prepared(
    rows: u16,
    cols: u16,
    cwd: &Path,
    argv: &[String],
    env: &[(String, String)],
    scrollback_limit_bytes: usize,
) -> io::Result<PreparedChild> {
    if argv.is_empty() {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "argv empty"));
    }
    let gate = std::env::temp_dir().join(format!("gterm-gate-{}", uuid::Uuid::new_v4()));
    unsafe {
        let path = std::ffi::CString::new(gate.to_string_lossy().as_bytes()).unwrap();
        if libc::mkfifo(path.as_ptr(), 0o600) != 0 {
            return Err(io::Error::last_os_error());
        }
    }
    let mut wrapped = vec![
        "/bin/sh".to_string(),
        "-c".to_string(),
        "read _ < \"$1\" && shift && exec \"$@\"".to_string(),
        "gterm-gate".to_string(),
        gate.to_string_lossy().into_owned(),
    ];
    wrapped.extend(argv.iter().cloned());
    let mut extra: Vec<(String, String)> = env.to_vec();
    extra.push((
        crate::GTERM_ENV_VAR.to_string(),
        crate::GTERM_ENV_VALUE.to_string(),
    ));
    let launch = PaneLaunchEnv::from_extra(extra);
    let runtime = PaneRuntime::spawn_argv_command(
        rows,
        cols,
        cwd.to_path_buf(),
        &wrapped,
        &launch,
        scrollback_limit_bytes,
        TerminalTheme::default(),
        None,
    )?;
    let pid = runtime.child_pid().unwrap_or(0);
    let start_time = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    if pid > 0 {
        unsafe {
            libc::setpgid(pid as i32, pid as i32);
        }
    }
    Ok(PreparedChild {
        runtime,
        pid,
        pgid: pid as i32,
        start_time,
        gate,
    })
}
