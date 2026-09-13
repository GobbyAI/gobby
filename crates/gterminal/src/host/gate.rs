//! `gterm gate`: an exec-status barrier for prepared PTY children.

#![cfg(unix)]

use std::ffi::{CString, OsStr, OsString};
use std::io;
use std::os::fd::RawFd;
use std::os::unix::ffi::OsStrExt;
use std::path::{Path, PathBuf};

use serde::Serialize;

pub(crate) const GATE_FD: RawFd = 3;
pub(crate) const STATUS_FD: RawFd = 4;
pub(crate) const PTY_FD: RawFd = 5;

#[derive(Serialize)]
struct GateFailure<'a> {
    code: &'a str,
    detail: String,
    stage: &'a str,
}

/// Run the gate process and replace it with `argv` after the host commits.
pub(crate) fn run(argv: Vec<OsString>) -> ! {
    let argv = match argv.split_first() {
        Some((program, args)) if !program.is_empty() => (program, args),
        _ => fail("arguments", io::Error::from_raw_os_error(libc::EINVAL)),
    };

    if injected_fault("setsid") {
        fail("setsid", io::Error::from_raw_os_error(libc::EINVAL));
    }
    if unsafe { libc::setsid() } < 0 {
        fail("setsid", io::Error::last_os_error());
    }
    if unsafe { libc::ioctl(PTY_FD, libc::TIOCSCTTY.into(), 0) } < 0 {
        fail("setsid", io::Error::last_os_error());
    }

    if injected_fault("dup2") {
        fail("dup2", io::Error::from_raw_os_error(libc::EBADF));
    }
    for target in [libc::STDIN_FILENO, libc::STDOUT_FILENO, libc::STDERR_FILENO] {
        if unsafe { libc::dup2(PTY_FD, target) } < 0 {
            fail("dup2", io::Error::last_os_error());
        }
    }
    close_fd(PTY_FD);

    wait_for_commit();
    close_fd(GATE_FD);

    if injected_fault("signal") {
        unsafe {
            libc::raise(libc::SIGKILL);
        }
        fail("signal", io::Error::last_os_error());
    }
    if injected_fault("timeout") {
        loop {
            unsafe {
                libc::pause();
            }
        }
    }
    if injected_fault("malformed") {
        write_all(STATUS_FD, b"{");
        unsafe {
            libc::_exit(127);
        }
    }
    if injected_fault("delay") {
        std::thread::sleep(std::time::Duration::from_millis(200));
    }

    let executable = if argv.0.as_bytes().contains(&b'/') {
        Ok(PathBuf::from(argv.0))
    } else {
        resolve_path(argv.0)
    };
    if injected_fault("PATH") {
        fail("PATH", io::Error::from_raw_os_error(libc::ENOENT));
    }
    let executable = executable.unwrap_or_else(|error| fail("PATH", error));

    let args = marshal_argv(argv.0, argv.1).unwrap_or_else(|error| fail("arguments", error));
    let env = marshal_env().unwrap_or_else(|error| fail("arguments", error));
    if let Err(error) = close_on_exec(STATUS_FD) {
        fail("execve", error);
    }
    reset_signals();

    let arg_ptrs = cstring_ptrs(&args);
    let env_ptrs = cstring_ptrs(&env);
    let executable = CString::new(executable.as_os_str().as_bytes())
        .unwrap_or_else(|_| fail("arguments", io::Error::from_raw_os_error(libc::EINVAL)));
    unsafe {
        libc::execve(executable.as_ptr(), arg_ptrs.as_ptr(), env_ptrs.as_ptr());
    }
    fail("execve", io::Error::last_os_error())
}

fn wait_for_commit() {
    let mut byte = 0_u8;
    loop {
        let read = unsafe { libc::read(GATE_FD, (&mut byte as *mut u8).cast(), 1) };
        if read == 1 {
            return;
        }
        if read == 0 {
            unsafe {
                libc::_exit(127);
            }
        }
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            fail("gate", error);
        }
    }
}

fn resolve_path(program: &OsStr) -> Result<PathBuf, io::Error> {
    let path = std::env::var_os("PATH").unwrap_or_default();
    for directory in std::env::split_paths(&path) {
        let candidate = directory.join(program);
        let candidate_c = CString::new(candidate.as_os_str().as_bytes())
            .map_err(|_| io::Error::from_raw_os_error(libc::EINVAL))?;
        if unsafe { libc::access(candidate_c.as_ptr(), libc::F_OK) } == 0 {
            return Ok(candidate);
        }
    }
    Err(io::Error::from_raw_os_error(libc::ENOENT))
}

fn marshal_argv(program: &OsStr, args: &[OsString]) -> Result<Vec<CString>, io::Error> {
    std::iter::once(program)
        .chain(args.iter().map(OsString::as_os_str))
        .map(|value| {
            CString::new(value.as_bytes()).map_err(|_| io::Error::from_raw_os_error(libc::EINVAL))
        })
        .collect()
}

fn marshal_env() -> Result<Vec<CString>, io::Error> {
    std::env::vars_os()
        .map(|(key, value)| {
            let mut entry = Vec::with_capacity(key.as_bytes().len() + value.as_bytes().len() + 1);
            entry.extend_from_slice(key.as_bytes());
            entry.push(b'=');
            entry.extend_from_slice(value.as_bytes());
            CString::new(entry).map_err(|_| io::Error::from_raw_os_error(libc::EINVAL))
        })
        .collect()
}

fn cstring_ptrs(values: &[CString]) -> Vec<*const libc::c_char> {
    values
        .iter()
        .map(|value| value.as_ptr())
        .chain(std::iter::once(std::ptr::null()))
        .collect()
}

fn close_on_exec(fd: RawFd) -> io::Result<()> {
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFD, flags | libc::FD_CLOEXEC) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

fn reset_signals() {
    for signal in [
        libc::SIGHUP,
        libc::SIGINT,
        libc::SIGQUIT,
        libc::SIGTERM,
        libc::SIGPIPE,
        libc::SIGCHLD,
    ] {
        unsafe {
            libc::signal(signal, libc::SIG_DFL);
        }
    }
}

fn fail(stage: &'static str, error: io::Error) -> ! {
    let errno = error.raw_os_error().unwrap_or(libc::EIO);
    let failure = GateFailure {
        code: errno_name(errno),
        detail: io::Error::from_raw_os_error(errno).to_string(),
        stage,
    };
    if let Ok(payload) = serde_json::to_vec(&failure) {
        write_all(STATUS_FD, &payload);
    }
    unsafe {
        libc::_exit(127);
    }
}

fn write_all(fd: RawFd, mut bytes: &[u8]) {
    while !bytes.is_empty() {
        let written = unsafe { libc::write(fd, bytes.as_ptr().cast(), bytes.len()) };
        if written > 0 {
            bytes = &bytes[written as usize..];
            continue;
        }
        if written < 0 && io::Error::last_os_error().kind() == io::ErrorKind::Interrupted {
            continue;
        }
        return;
    }
}

fn close_fd(fd: RawFd) {
    unsafe {
        libc::close(fd);
    }
}

pub(crate) fn errno_name(errno: i32) -> &'static str {
    match errno {
        libc::EACCES => "EACCES",
        libc::EBADF => "EBADF",
        libc::EFAULT => "EFAULT",
        libc::EINVAL => "EINVAL",
        libc::EIO => "EIO",
        libc::ENOENT => "ENOENT",
        libc::ENOEXEC => "ENOEXEC",
        libc::ENOMEM => "ENOMEM",
        libc::ENOTDIR => "ENOTDIR",
        libc::EPERM => "EPERM",
        libc::EPIPE => "EPIPE",
        _ => "EIO",
    }
}

#[cfg(debug_assertions)]
fn injected_fault(expected: &str) -> bool {
    std::env::var_os("GTERM_TEST_HELPER").is_some_and(|value| value == "1")
        && std::env::var_os("GTERM_GATE_FAULT").is_some_and(|value| value == expected)
}

#[cfg(not(debug_assertions))]
fn injected_fault(_expected: &str) -> bool {
    false
}
