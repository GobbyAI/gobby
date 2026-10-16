//! Process identity for the gterm host: a live, non-zombie process named gterm.
//! Identity is read in-process, so it needs no exec. The macOS libproc calls are
//! the crate's only unsafe code.
#![allow(unsafe_code)]

use super::{HostFailure, pre_io};

#[cfg(test)]
mod tests;

const HOST_NAME: &str = "gterm";

pub(super) async fn process_is_host(pid: u32) -> Result<bool, HostFailure> {
    // Pid 0 and pids past i32::MAX never name a host process.
    match i32::try_from(pid) {
        Ok(pid) if pid > 0 => identity(pid).await,
        _ => Ok(false),
    }
}

#[cfg(target_os = "macos")]
async fn identity(pid: i32) -> Result<bool, HostFailure> {
    use std::os::unix::ffi::OsStrExt;
    /// `SZOMB` in `<sys/proc.h>`.
    const SZOMB: u32 = 5;

    let size = std::mem::size_of::<libc::proc_bsdinfo>() as libc::c_int;
    // SAFETY: proc_bsdinfo is a plain C struct, so all-zero bytes are a valid value.
    let mut info: libc::proc_bsdinfo = unsafe { std::mem::zeroed() };
    // SAFETY: `info` is writable, lives through the call, and `size` is its exact size.
    let read =
        unsafe { libc::proc_pidinfo(pid, libc::PROC_PIDTBSDINFO, 0, (&raw mut info).cast(), size) };
    if read <= 0 {
        return absent_or_failure(std::io::Error::last_os_error());
    }
    if read != size {
        return Err(HostFailure::PreAdoptionIo(format!(
            "process info read {read} of {size} bytes"
        )));
    }
    if info.pbi_status == SZOMB {
        return Ok(false);
    }

    let mut path = [0u8; libc::PROC_PIDPATHINFO_MAXSIZE as usize];
    // SAFETY: `path` is writable, lives through the call, and its exact length is passed.
    let len = unsafe { libc::proc_pidpath(pid, path.as_mut_ptr().cast(), path.len() as u32) };
    let Some(path) = usize::try_from(len)
        .ok()
        .filter(|len| *len > 0)
        .and_then(|len| path.get(..len))
    else {
        return absent_or_failure(std::io::Error::last_os_error());
    };
    Ok(std::path::Path::new(std::ffi::OsStr::from_bytes(path))
        .file_name()
        .is_some_and(|name| name == HOST_NAME))
}

#[cfg(not(target_os = "macos"))]
async fn identity(pid: i32) -> Result<bool, HostFailure> {
    let stat = match tokio::fs::read_to_string(format!("/proc/{pid}/stat")).await {
        Ok(stat) => stat,
        Err(error) => return absent_or_failure(error),
    };
    // `pid (comm) state ...`; comm may itself contain parentheses.
    let (Some(open), Some(close)) = (stat.find('('), stat.rfind(')')) else {
        return Err(HostFailure::PidMismatch);
    };
    let (Some(comm), Some(state)) = (
        stat.get(open + 1..close),
        stat.get(close + 1..)
            .and_then(|rest| rest.trim_start().chars().next()),
    ) else {
        return Err(HostFailure::PidMismatch);
    };
    Ok(state != 'Z' && comm == HOST_NAME)
}

/// ESRCH or a vanished /proc entry proves absence; anything else is an I/O failure.
fn absent_or_failure(error: std::io::Error) -> Result<bool, HostFailure> {
    if error.kind() == std::io::ErrorKind::NotFound
        || rustix::io::Errno::from_io_error(&error) == Some(rustix::io::Errno::SRCH)
    {
        Ok(false)
    } else {
        Err(pre_io(error))
    }
}
