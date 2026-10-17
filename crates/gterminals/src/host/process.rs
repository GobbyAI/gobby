//! Process identity for the gterm host: a live, non-zombie process named gterm.
//! Identity is read in-process, so it needs no exec. The macOS libproc call is
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

/// Reads the exec-time command name, which survives promotion replacing or
/// unlinking the running host's executable.
#[cfg(target_os = "macos")]
async fn identity(pid: i32) -> Result<bool, HostFailure> {
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
    // p_comm fills MAXCOMLEN bytes and is NUL-terminated only when shorter.
    let comm = info.pbi_comm.map(|byte| byte as u8);
    let name = comm.split(|byte| *byte == 0).next().unwrap_or_default();
    Ok(name == HOST_NAME.as_bytes())
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

/// Only ESRCH proves a pid is gone, plus a vanished /proc entry off macOS;
/// anything else is an I/O failure.
fn absent_or_failure(error: std::io::Error) -> Result<bool, HostFailure> {
    let vanished_proc_entry =
        cfg!(not(target_os = "macos")) && error.kind() == std::io::ErrorKind::NotFound;
    if vanished_proc_entry
        || rustix::io::Errno::from_io_error(&error) == Some(rustix::io::Errno::SRCH)
    {
        Ok(false)
    } else {
        Err(pre_io(error))
    }
}
