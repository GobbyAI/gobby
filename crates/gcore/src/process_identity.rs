//! The CLI process that owns a hook or an MCP client.
//!
//! A CLI's hook and its MCP client are both children of the CLI process, so their
//! parent pid names the CLI. Droid's TUI runs a `droid exec` backend child that runs
//! the hooks and is replaced on `/compress` and `/clear`; the TUI is the terminal's
//! stable identity. This mirrors `_stable_cli_process` in the Python hook tooling.

/// What the process table reports for one process.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcessInfo {
    /// The parent pid, or `None` when the process has no parent.
    pub parent: Option<u32>,
    /// The short process name the table keeps, such as `droid`.
    pub name: String,
    /// The argument vector. `None` when it cannot be read, which is not the same as
    /// an empty list.
    pub argv: Option<Vec<String>>,
}

const DROID: &str = "droid";

/// The stable CLI pid for `pid`, or `None` when `pid`'s own process info is unreadable.
pub fn stable_cli_pid(pid: u32) -> Option<u32> {
    stable_cli_pid_with(pid, process_info)
}

/// The stable CLI pid for `pid`, reading each process through `lookup`.
///
/// A Droid process whose parent is a Droid TUI resolves to that TUI, unless the
/// parent is itself a headless `droid exec`. An unreadable parent keeps `pid`.
pub fn stable_cli_pid_with(pid: u32, lookup: impl Fn(u32) -> Option<ProcessInfo>) -> Option<u32> {
    let process = lookup(pid)?;
    if process.name != DROID {
        return Some(pid);
    }
    let Some(parent_pid) = process.parent else {
        return Some(pid);
    };
    let Some(parent) = lookup(parent_pid) else {
        return Some(pid);
    };
    let is_tui_parent = parent.name == DROID
        && parent
            .argv
            .as_ref()
            .is_some_and(|argv| argv.get(1).map(String::as_str) != Some("exec"));
    Some(if is_tui_parent { parent_pid } else { pid })
}

/// The stable CLI pid of the process that spawned this one, if it can be read.
pub fn current_cli_pid() -> Option<u32> {
    stable_cli_pid(parent_pid()?)
}

const CODEX: &str = "codex";
const CODEX_SHARED_HOST_FLAG: &str = "--managed-daemon";

/// Whether `process` is Codex's shared managed app-server worker.
///
/// That worker runs every hand-started thread's hooks and MCP clients, so its pid names
/// no single session. This mirrors `_is_codex_shared_host` in the Python hook tooling.
pub fn is_codex_shared_host(process: &ProcessInfo) -> bool {
    process.name == CODEX
        && process
            .argv
            .as_ref()
            .is_some_and(|argv| argv.iter().any(|arg| arg == CODEX_SHARED_HOST_FLAG))
}

/// Whether the process that spawned this one is Codex's shared app-server worker.
pub fn parent_is_codex_shared_host() -> bool {
    parent_pid()
        .and_then(process_info)
        .is_some_and(|parent| is_codex_shared_host(&parent))
}

#[cfg(unix)]
fn parent_pid() -> Option<u32> {
    use libc::getppid;

    // SAFETY: getppid has no preconditions and cannot fail.
    let pid = unsafe { getppid() };
    u32::try_from(pid).ok().filter(|pid| *pid != 0)
}

#[cfg(not(unix))]
fn parent_pid() -> Option<u32> {
    None
}

#[cfg(target_os = "macos")]
fn process_info(pid: u32) -> Option<ProcessInfo> {
    let (parent, name) = macos::bsd_info(pid)?;
    Some(ProcessInfo {
        parent: parent.filter(|parent| *parent != 0),
        name,
        argv: macos::argv(pid),
    })
}

#[cfg(target_os = "linux")]
fn process_info(pid: u32) -> Option<ProcessInfo> {
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let (name, parent) = parse_stat(&stat)?;
    let argv = std::fs::read(format!("/proc/{pid}/cmdline"))
        .ok()
        .map(|bytes| parse_cmdline(&bytes));
    Some(ProcessInfo {
        parent: Some(parent).filter(|parent| *parent != 0),
        name,
        argv,
    })
}

#[cfg(not(any(target_os = "linux", target_os = "macos")))]
fn process_info(_pid: u32) -> Option<ProcessInfo> {
    None
}

/// Parse `/proc/<pid>/stat` into the command name and parent pid.
///
/// The command name is wrapped in parentheses and may itself contain spaces or
/// parentheses, so the name ends at the last `)`.
#[cfg(any(target_os = "linux", test))]
fn parse_stat(stat: &str) -> Option<(String, u32)> {
    let open = stat.find('(')?;
    let close = stat.rfind(')')?;
    let name = stat.get(open + 1..close)?.to_owned();
    let mut fields = stat.get(close + 1..)?.split_whitespace();
    let _state = fields.next()?;
    let parent = fields.next()?.parse().ok()?;
    Some((name, parent))
}

/// Split a NUL-terminated `/proc/<pid>/cmdline` into its arguments.
#[cfg(any(target_os = "linux", test))]
fn parse_cmdline(bytes: &[u8]) -> Vec<String> {
    if bytes.is_empty() {
        return Vec::new();
    }
    let body = bytes.strip_suffix(&[0_u8]).unwrap_or(bytes);
    body.split(|&byte| byte == 0)
        .map(|arg| String::from_utf8_lossy(arg).into_owned())
        .collect()
}

/// Read the arguments from a `KERN_PROCARGS2` buffer: the argument count as a native
/// `i32`, the executable path, NUL padding, then that many NUL-terminated arguments.
#[cfg(any(target_os = "macos", test))]
fn parse_procargs2(buffer: &[u8]) -> Option<Vec<String>> {
    let argc = usize::try_from(i32::from_ne_bytes(buffer.get(..4)?.try_into().ok()?)).ok()?;
    let rest = buffer.get(4..)?;
    let exec_end = rest.iter().position(|&byte| byte == 0)?;
    let mut cursor = &rest[exec_end..];
    cursor = &cursor[cursor
        .iter()
        .position(|&byte| byte != 0)
        .unwrap_or(cursor.len())..];
    let mut argv = Vec::new();
    for _ in 0..argc {
        let end = cursor.iter().position(|&byte| byte == 0)?;
        argv.push(String::from_utf8_lossy(&cursor[..end]).into_owned());
        cursor = &cursor[end + 1..];
    }
    Some(argv)
}

#[cfg(target_os = "macos")]
mod macos {
    use std::ffi::c_void;
    use std::mem::{MaybeUninit, size_of};
    use std::ptr;

    use libc::c_int;

    /// The parent pid and short name the kernel reports for one process.
    pub(super) fn bsd_info(pid: u32) -> Option<(Option<u32>, String)> {
        let pid = c_int::try_from(pid).ok()?;
        let size = c_int::try_from(size_of::<libc::proc_bsdinfo>()).ok()?;
        let mut info = MaybeUninit::<libc::proc_bsdinfo>::zeroed();
        // SAFETY: `info` holds exactly one proc_bsdinfo and `size` is its size, so the
        // kernel writes only inside it.
        let written = unsafe {
            libc::proc_pidinfo(
                pid,
                libc::PROC_PIDTBSDINFO,
                0,
                info.as_mut_ptr().cast::<c_void>(),
                size,
            )
        };
        if written != size {
            return None;
        }
        // SAFETY: zeroed is a valid bit pattern for this struct, and a full-size write
        // initialized the fields the kernel fills.
        let info = unsafe { info.assume_init() };
        let comm = info.pbi_comm;
        // SAFETY: pbi_comm is an inline array of MAXCOMLEN one-byte c_char values, so
        // viewing its bytes as u8 stays in bounds and every byte is initialized.
        let bytes = unsafe { std::slice::from_raw_parts(comm.as_ptr().cast::<u8>(), comm.len()) };
        let end = bytes
            .iter()
            .position(|&byte| byte == 0)
            .unwrap_or(bytes.len());
        Some((
            Some(info.pbi_ppid),
            String::from_utf8_lossy(&bytes[..end]).into_owned(),
        ))
    }

    /// The argument vector from `KERN_PROCARGS2`, or `None` when the kernel refuses it.
    pub(super) fn argv(pid: u32) -> Option<Vec<String>> {
        let pid = c_int::try_from(pid).ok()?;
        let mut buffer = vec![0_u8; argmax()?];
        let mut length = buffer.len();
        let mut mib = [libc::CTL_KERN, libc::KERN_PROCARGS2, pid];
        // SAFETY: mib names three ints, `buffer` is writable for `length` bytes, and no
        // new value is supplied.
        let status = unsafe {
            libc::sysctl(
                mib.as_mut_ptr(),
                3,
                buffer.as_mut_ptr().cast::<c_void>(),
                &mut length,
                ptr::null_mut(),
                0,
            )
        };
        if status != 0 {
            return None;
        }
        buffer.truncate(length);
        super::parse_procargs2(&buffer)
    }

    /// The kernel's largest argument buffer, from `KERN_ARGMAX`.
    fn argmax() -> Option<usize> {
        let mut argmax: c_int = 0;
        let mut length = size_of::<c_int>();
        let mut mib = [libc::CTL_KERN, libc::KERN_ARGMAX];
        // SAFETY: mib names two ints, `argmax` is one writable c_int for `length` bytes,
        // and no new value is supplied.
        let status = unsafe {
            libc::sysctl(
                mib.as_mut_ptr(),
                2,
                (&mut argmax as *mut c_int).cast::<c_void>(),
                &mut length,
                ptr::null_mut(),
                0,
            )
        };
        if status != 0 {
            return None;
        }
        usize::try_from(argmax).ok()
    }
}

#[cfg(test)]
#[path = "process_identity/tests.rs"]
mod tests;
