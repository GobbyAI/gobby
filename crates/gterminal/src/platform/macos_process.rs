//! macOS process inspection: group membership, argv, cwd, and session walks.
use std::ffi::OsStr;
use std::os::fd::RawFd;
use std::os::unix::ffi::OsStrExt;
use std::path::PathBuf;

/// Foreground process-group id for the given PID.
pub fn foreground_process_group_id(pid: u32) -> Option<u32> {
    let mut info: libc::proc_bsdinfo = unsafe { std::mem::zeroed() };
    let size = std::mem::size_of::<libc::proc_bsdinfo>() as libc::c_int;

    let ret = unsafe {
        libc::proc_pidinfo(
            pid as libc::c_int,
            libc::PROC_PIDTBSDINFO,
            0,
            &mut info as *mut _ as *mut libc::c_void,
            size,
        )
    };

    if ret != size {
        return None;
    }

    let fg = info.e_tpgid;
    if fg > 0 {
        // reason: e_tpgid is pid_t; the u32 cast width is platform-dependent.
        #[allow(clippy::unnecessary_cast)]
        Some(fg as u32)
    } else {
        None
    }
}

pub fn foreground_process_group_id_for_tty_fd(fd: RawFd) -> Option<u32> {
    let pgid = unsafe { libc::tcgetpgrp(fd) };
    (pgid > 0).then_some(pgid as u32)
}

#[cfg(test)]
fn procargs2_argv_start(rest: &[u8]) -> Option<usize> {
    let exec_end = rest.iter().position(|&byte| byte == 0)?;
    let mut pos = exec_end;
    while pos < rest.len() && rest[pos] == 0 {
        pos += 1;
    }
    (pos < rest.len()).then_some(pos)
}

#[cfg(test)]
pub fn procargs2_argv(buf: &[u8]) -> Option<Vec<String>> {
    if buf.len() < 4 {
        return None;
    }

    let argc = i32::from_ne_bytes([buf[0], buf[1], buf[2], buf[3]]);
    if argc < 1 {
        return None;
    }

    // Layout: [argc: i32] [exec_path\0] [padding\0...] [argv[0]\0] ... [env\0] ...
    let rest = &buf[4..];
    let mut current = procargs2_argv_start(rest)?;
    let mut argv = Vec::with_capacity(argc as usize);
    for _ in 0..argc {
        if current >= rest.len() {
            return None;
        }
        let end = rest[current..]
            .iter()
            .position(|&b| b == 0)
            .map(|offset| current + offset)
            .unwrap_or(rest.len());
        if end == current {
            return None;
        }
        argv.push(String::from_utf8_lossy(&rest[current..end]).into_owned());
        current = end + 1;
    }

    Some(argv)
}

/// Uses `proc_pidinfo(PROC_PIDVNODEPATHINFO)` to read `pvi_cdir.vip_path`.
pub fn process_cwd(pid: u32) -> Option<PathBuf> {
    if pid == 0 {
        return None;
    }

    let mut pathinfo: libc::proc_vnodepathinfo = unsafe { std::mem::zeroed() };
    let size = std::mem::size_of::<libc::proc_vnodepathinfo>() as libc::c_int;

    let ret = unsafe {
        libc::proc_pidinfo(
            pid as libc::c_int,
            libc::PROC_PIDVNODEPATHINFO,
            0,
            &mut pathinfo as *mut _ as *mut libc::c_void,
            size,
        )
    };

    if ret != size {
        return None;
    }

    // vip_path is [[c_char; 32]; 32] in libc (workaround for old Rust const generics).
    // Reinterpret as flat bytes (total MAXPATHLEN = 1024).
    let vip_path = unsafe {
        std::slice::from_raw_parts(
            pathinfo.pvi_cdir.vip_path.as_ptr() as *const u8,
            libc::MAXPATHLEN as usize,
        )
    };

    let nul = vip_path.iter().position(|&b| b == 0)?;
    if nul == 0 {
        return None;
    }
    Some(PathBuf::from(OsStr::from_bytes(&vip_path[..nul])))
}

pub fn session_processes(child_pid: u32) -> Vec<u32> {
    if child_pid == 0 {
        return Vec::new();
    }

    let target_session = unsafe { libc::getsid(child_pid as libc::c_int) };
    if target_session <= 0 {
        return Vec::new();
    }

    all_pids()
        .into_iter()
        .filter(|pid| unsafe { libc::getsid(*pid as libc::pid_t) } == target_session)
        .collect()
}

fn all_pids() -> Vec<u32> {
    let initial_count = unsafe { libc::proc_listallpids(std::ptr::null_mut(), 0) };
    let mut capacity = if initial_count > 0 {
        initial_count as usize + 128
    } else {
        4096
    };

    for _ in 0..8 {
        let mut pids = vec![0 as libc::pid_t; capacity];
        let count = unsafe {
            libc::proc_listallpids(
                pids.as_mut_ptr() as *mut libc::c_void,
                (pids.len() * std::mem::size_of::<libc::pid_t>()) as libc::c_int,
            )
        };
        if count <= 0 {
            return Vec::new();
        }

        let count = count as usize;
        if count < capacity {
            return collect_positive_pids(pids, count);
        }
        capacity = capacity.saturating_mul(2);
    }

    Vec::new()
}

fn collect_positive_pids(pids: Vec<libc::pid_t>, count: usize) -> Vec<u32> {
    pids.into_iter()
        .take(count)
        .filter(|pid| *pid > 0)
        .map(|pid| pid as u32)
        .collect()
}
