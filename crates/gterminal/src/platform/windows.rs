use std::{
    cmp::Ordering,
    collections::{HashMap, HashSet, VecDeque},
    ffi::{c_void, OsStr},
    mem::{size_of, MaybeUninit},
    path::PathBuf,
    ptr::{copy_nonoverlapping, null_mut},
    sync::{
        atomic::{AtomicU64, Ordering as AtomicOrdering},
        Arc, LazyLock, Mutex,
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use windows_sys::{
    Wdk::System::Threading::{NtQueryInformationProcess, ProcessBasicInformation},
    Win32::{
        Foundation::{
            CloseHandle, GlobalFree, LocalFree, FILETIME, HANDLE, HWND, INVALID_HANDLE_VALUE,
            NTSTATUS, STATUS_SUCCESS, UNICODE_STRING,
        },
        Globalization::{CompareStringOrdinal, CSTR_EQUAL, CSTR_GREATER_THAN, CSTR_LESS_THAN},
        System::{
            Console::GetConsoleWindow,
            DataExchange::{CloseClipboard, EmptyClipboard, OpenClipboard, SetClipboardData},
            Diagnostics::{
                Debug::ReadProcessMemory,
                ToolHelp::{
                    CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W,
                    TH32CS_SNAPPROCESS,
                },
            },
            JobObjects::{
                IsProcessInJob, JobObjectExtendedLimitInformation, QueryInformationJobObject,
                JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
            },
            Memory::{
                GlobalAlloc, GlobalLock, GlobalUnlock, VirtualQueryEx, GMEM_MOVEABLE,
                MEMORY_BASIC_INFORMATION,
            },
            Ole::CF_UNICODETEXT,
            Threading::{
                GetCurrentProcess, GetExitCodeProcess, GetProcessTimes, OpenProcess,
                QueryFullProcessImageNameW, TerminateProcess, CREATE_NO_WINDOW, DETACHED_PROCESS,
                PROCESS_BASIC_INFORMATION, PROCESS_QUERY_INFORMATION,
                PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_VM_READ,
            },
        },
        UI::{
            Input::{
                Ime::ImmGetDefaultIMEWnd,
                KeyboardAndMouse::{
                    GetKeyboardLayout, SendInput, INPUT, INPUT_0, INPUT_KEYBOARD, KEYBDINPUT,
                    KEYEVENTF_KEYUP,
                },
            },
            Shell::{
                CommandLineToArgvW, ShellExecuteW, Shell_NotifyIconW, NIF_ICON, NIF_INFO, NIF_TIP,
                NIIF_INFO, NIIF_NOSOUND, NIM_ADD, NIM_DELETE, NIM_MODIFY, NOTIFYICONDATAW,
            },
            WindowsAndMessaging::{
                CreateWindowExW, DestroyWindow, GetForegroundWindow, GetWindowThreadProcessId,
                LoadIconW, SendMessageTimeoutW, IDI_APPLICATION, SMTO_ABORTIFHUNG, WM_IME_CONTROL,
            },
        },
    },
};

use super::{ClipboardImage, ForegroundJob, Signal};

const STILL_ACTIVE: u32 = 259;
const FOREGROUND_PROCESS_SNAPSHOT_CACHE_TTL: Duration = Duration::from_millis(250);
const PANE_RUNTIME_MARKER_ENV_VAR: &str = "GTERM_PANE_RUNTIME_ID";
const MAX_PROCESS_ENVIRONMENT_BYTES: usize = 256 * 1024;
const PROCESS_ENVIRONMENT_READ_CHUNK_BYTES: usize = 16 * 1024;
const PROCESS_RUNTIME_MARKER_CACHE_CAPACITY: usize = 1_024;
const PROCESS_RUNTIME_MARKER_CACHE_RETENTION: Duration = Duration::from_secs(60);
const PROCESS_RUNTIME_MARKER_NEGATIVE_TTL: Duration = Duration::from_secs(1);

static NEXT_PANE_RUNTIME_MARKER: AtomicU64 = AtomicU64::new(1);
static PROCESS_RUNTIME_MARKER_CACHE: LazyLock<Mutex<HashMap<u32, CachedProcessRuntimeMarker>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));
static GIT_BASH_PROCESS_CACHE: LazyLock<Mutex<HashMap<u32, CachedGitBashProcess>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

/// Encode native or targeted semantic Win32 input for a compatible ConPTY destination.
pub(crate) fn encode_windows_conpty_fallback(key: &crate::input::TerminalKey) -> Option<Vec<u8>> {
    use crossterm::event::{KeyCode, KeyEventKind, KeyModifiers};

    let (virtual_key_code, virtual_scan_code, unicode, control_key_state) =
        if let Some(record) = key.windows_record() {
            (
                record.virtual_key_code,
                record.virtual_scan_code,
                record.unicode,
                record.control_key_state,
            )
        } else if key.code == KeyCode::Esc
            && key.modifiers.is_empty()
            && key.kind == KeyEventKind::Press
            && key.vt_bytes().is_none()
        {
            return Some(b"\x1b[27;1;27;1;0;1_\x1b[27;1;27;0;0;1_".to_vec());
        } else if key.code == KeyCode::Enter && key.modifiers == KeyModifiers::SHIFT {
            (13, 28, 13, 16)
        } else {
            return None;
        };
    let key_down = key.kind != KeyEventKind::Release;
    let repeat_count = if key_down { key.repeat_count.max(1) } else { 1 };

    Some(
        format!(
            "\x1b[{virtual_key_code};{virtual_scan_code};{unicode};{};{control_key_state};{repeat_count}_",
            u8::from(key_down),
        )
        .into_bytes(),
    )
}

#[derive(Debug)]
struct CachedProcessSnapshot {
    built_at: Instant,
    entries: Arc<Vec<WindowsProcessEntry>>,
}

#[derive(Debug)]
struct ProcessSnapshotCache {
    cached: Option<CachedProcessSnapshot>,
}

#[derive(Debug)]
struct CachedProcessRuntimeMarker {
    creation_time: u64,
    marker: Option<String>,
    cached_at: Instant,
    last_used: Instant,
}

#[derive(Debug)]
struct CachedGitBashProcess {
    creation_time: u64,
    is_git_bash: bool,
    last_used: Instant,
}

static FOREGROUND_PROCESS_SNAPSHOT_CACHE: Mutex<ProcessSnapshotCache> =
    Mutex::new(ProcessSnapshotCache { cached: None });

pub(crate) fn should_draw_host_cursor_by_default() -> bool {
    true
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct WindowsProcessEntry {
    pid: u32,
    parent_pid: u32,
    name: String,
    argv0: Option<String>,
    argv: Option<Vec<String>>,
    cmdline: Option<String>,
}

pub fn raise_server_nofile_limit() {}

pub(crate) fn apply_pane_runtime_marker_platform(command: &mut portable_pty::CommandBuilder) {
    if command_uses_git_bash(command) {
        command.env(PANE_RUNTIME_MARKER_ENV_VAR, next_pane_runtime_marker());
    }
}

fn next_pane_runtime_marker() -> String {
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);
    let counter = NEXT_PANE_RUNTIME_MARKER.fetch_add(1, AtomicOrdering::Relaxed);
    format!("{:x}-{timestamp:x}-{counter:x}", std::process::id())
}

fn raw_command_shell(comspec: Option<std::ffi::OsString>) -> std::ffi::OsString {
    comspec
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| r"C:\Windows\System32\cmd.exe".into())
}

pub(crate) fn interactive_shell_command(argv: &[String], shell_name: &str) -> Option<String> {
    let shell_name = shell_name.to_ascii_lowercase();
    let powershell = shell_name.contains("powershell") || shell_name.contains("pwsh");
    let script = powershell_agent_script(argv)?;
    if powershell {
        Some(script)
    } else {
        Some(cmd_encoded_powershell_command(&script))
    }
}

fn powershell_agent_script(argv: &[String]) -> Option<String> {
    let (program, args) = argv.split_first()?;
    if args.is_empty() {
        return Some(format!("& {}", super::quote_powershell_arg(program)));
    }

    let command_line = args
        .iter()
        .map(|arg| quote_windows_command_line_arg(arg))
        .collect::<Vec<_>>()
        .join(" ");
    Some(format!(
        "$p=Start-Process -FilePath {} -ArgumentList {} -NoNewWindow -Wait -PassThru",
        super::quote_powershell_arg(program),
        super::quote_powershell_arg(&command_line),
    ))
}

fn quote_windows_command_line_arg(value: &str) -> String {
    if !value.is_empty()
        && !value
            .chars()
            .any(|ch| matches!(ch, ' ' | '\t' | '\n' | '\x0b' | '"'))
    {
        return value.to_string();
    }

    let mut quoted = String::from("\"");
    let mut backslashes = 0;
    for ch in value.chars() {
        if ch == '\\' {
            backslashes += 1;
            continue;
        }
        if ch == '"' {
            quoted.push_str(&"\\".repeat(backslashes * 2 + 1));
        } else {
            quoted.push_str(&"\\".repeat(backslashes));
        }
        backslashes = 0;
        quoted.push(ch);
    }
    quoted.push_str(&"\\".repeat(backslashes * 2));
    quoted.push('"');
    quoted
}

fn cmd_encoded_powershell_command(script: &str) -> String {
    use base64::Engine as _;

    let utf16 = script
        .encode_utf16()
        .flat_map(u16::to_le_bytes)
        .collect::<Vec<_>>();
    let encoded = base64::engine::general_purpose::STANDARD.encode(utf16);
    format!("powershell.exe -NoLogo -NoProfile -EncodedCommand {encoded}")
}

pub(crate) fn detached_custom_command_process_platform(command: &str) -> std::process::Command {
    detached_custom_command_process_with_comspec(command, std::env::var_os("ComSpec"))
}

fn detached_custom_command_process_with_comspec(
    command: &str,
    comspec: Option<std::ffi::OsString>,
) -> std::process::Command {
    use std::os::windows::process::CommandExt;

    let mut process = std::process::Command::new(raw_command_shell(comspec));
    process.arg("/d").arg("/c").raw_arg(command);
    process
}

pub(crate) fn pane_custom_command_pty_builder_platform(
    command: &str,
) -> portable_pty::CommandBuilder {
    pane_custom_command_pty_builder_with_comspec(command, std::env::var_os("ComSpec"))
}

fn pane_custom_command_pty_builder_with_comspec(
    command: &str,
    comspec: Option<std::ffi::OsString>,
) -> portable_pty::CommandBuilder {
    let mut builder = portable_pty::CommandBuilder::new(raw_command_shell(comspec));
    builder.arg("/d");
    builder.arg("/c");
    builder.raw_arg(command);
    builder
}

pub(crate) fn scrollback_editor_argv(path: &std::path::Path) -> std::io::Result<Vec<String>> {
    let editor = std::env::var("VISUAL")
        .ok()
        .filter(|value| !value.trim().is_empty())
        .or_else(|| {
            std::env::var("EDITOR")
                .ok()
                .filter(|value| !value.trim().is_empty())
        });
    scrollback_editor_argv_with_env(path, editor.as_deref())
}

fn scrollback_editor_argv_with_env(
    path: &std::path::Path,
    editor: Option<&str>,
) -> std::io::Result<Vec<String>> {
    let mut argv = match editor.filter(|value| !value.trim().is_empty()) {
        Some(editor) => command_line_to_argv(editor).ok_or_else(|| {
            std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                format!("failed to parse editor command {editor:?}"),
            )
        })?,
        None => vec!["notepad.exe".to_string()],
    };
    if argv.is_empty() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "editor command must not be empty",
        ));
    }
    argv.push(path.display().to_string());
    Ok(argv)
}

pub(crate) fn configure_background_command_platform(command: &mut std::process::Command) {
    use std::os::windows::process::CommandExt;

    command.creation_flags(CREATE_NO_WINDOW);
}

pub fn launch_server_daemon_command(command: &mut std::process::Command) -> std::io::Result<u32> {
    if current_job_kills_processes_on_close()? {
        launch_server_daemon_with_wmi(command)
    } else {
        command.spawn().map(|child| child.id())
    }
}

fn launch_server_daemon_with_wmi(command: &std::process::Command) -> std::io::Result<u32> {
    // WMI resolves the class from this Rust type name, including CIM casing.
    #[allow(non_camel_case_types)]
    #[derive(serde::Deserialize)]
    struct Win32_Process;

    // WMI serializes this embedded object using the matching CIM class name.
    #[allow(non_camel_case_types)]
    #[derive(serde::Serialize)]
    struct Win32_ProcessStartup {
        #[serde(rename = "CreateFlags")]
        create_flags: u32,
        #[serde(rename = "EnvironmentVariables")]
        environment_variables: Vec<String>,
    }

    #[derive(serde::Serialize)]
    struct CreateInput {
        #[serde(rename = "CommandLine")]
        command_line: String,
        #[serde(rename = "CurrentDirectory")]
        current_directory: String,
        #[serde(rename = "ProcessStartupInformation")]
        process_startup_information: Win32_ProcessStartup,
    }

    #[derive(serde::Deserialize)]
    struct CreateOutput {
        #[serde(rename = "ProcessId")]
        process_id: Option<u32>,
        #[serde(rename = "ReturnValue")]
        return_value: u32,
    }

    let current_directory = command
        .get_current_dir()
        .map(std::path::Path::to_path_buf)
        .map(Ok)
        .unwrap_or_else(std::env::current_dir)?;
    let input = CreateInput {
        command_line: windows_command_line(command)?,
        current_directory: unicode_windows_value(
            &current_directory.into_os_string(),
            "working directory",
        )?,
        process_startup_information: Win32_ProcessStartup {
            create_flags: DETACHED_PROCESS,
            environment_variables: effective_command_environment(command)?,
        },
    };

    let connection = wmi::WMIConnection::new()
        .map_err(|err| std::io::Error::other(format!("failed to connect to WMI: {err}")))?;
    let output: CreateOutput = connection
        .exec_class_method::<Win32_Process, _>("Create", &input)
        .map_err(|err| std::io::Error::other(format!("WMI Win32_Process.Create failed: {err}")))?;
    if output.return_value != 0 {
        return Err(std::io::Error::other(format!(
            "WMI Win32_Process.Create returned error {}",
            output.return_value
        )));
    }
    output.process_id.ok_or_else(|| {
        std::io::Error::other("WMI Win32_Process.Create succeeded without a process id")
    })
}

fn windows_command_line(command: &std::process::Command) -> std::io::Result<String> {
    std::iter::once(command.get_program())
        .chain(command.get_args())
        .map(|value| {
            unicode_windows_value(value, "server command argument")
                .map(|value| quote_windows_command_line_arg(&value))
        })
        .collect::<std::io::Result<Vec<_>>>()
        .map(|parts| parts.join(" "))
}

fn effective_command_environment(command: &std::process::Command) -> std::io::Result<Vec<String>> {
    let mut environment = std::env::vars_os()
        .map(|(key, value)| {
            Ok((
                unicode_windows_value(&key, "inherited environment variable name")?,
                unicode_windows_value(&value, "inherited environment variable value")?,
            ))
        })
        .collect::<std::io::Result<Vec<(String, String)>>>()?;
    for (key, value) in command.get_envs() {
        let key = unicode_windows_value(key, "environment variable name")?;
        environment.retain(|(inherited, _)| windows_environment_key_cmp(inherited, &key).is_ne());
        if let Some(value) = value {
            environment.push((
                key,
                unicode_windows_value(value, "environment variable value")?,
            ));
        }
    }
    environment.sort_unstable_by(|(left, _), (right, _)| windows_environment_key_cmp(left, right));
    Ok(environment
        .into_iter()
        .map(|(key, value)| format!("{key}={value}"))
        .collect())
}

fn windows_environment_key_cmp(left: &str, right: &str) -> Ordering {
    let left_wide: Vec<u16> = left.encode_utf16().collect();
    let right_wide: Vec<u16> = right.encode_utf16().collect();
    // SAFETY: both pointers remain valid for the call and lengths count UTF-16 units.
    match unsafe {
        CompareStringOrdinal(
            left_wide.as_ptr(),
            left_wide.len() as i32,
            right_wide.as_ptr(),
            right_wide.len() as i32,
            1,
        )
    } {
        CSTR_LESS_THAN => Ordering::Less,
        CSTR_EQUAL => Ordering::Equal,
        CSTR_GREATER_THAN => Ordering::Greater,
        _ => left.cmp(right),
    }
}

fn unicode_windows_value(value: &OsStr, label: &str) -> std::io::Result<String> {
    value.to_str().map(str::to_owned).ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            format!("{label} is not valid Unicode"),
        )
    })
}

fn current_process_is_in_job() -> std::io::Result<bool> {
    let mut in_job = 0;
    // SAFETY: `in_job` is a valid writable BOOL for the duration of the call.
    if unsafe { IsProcessInJob(GetCurrentProcess(), null_mut(), &mut in_job) } == 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(in_job != 0)
}

fn current_job_kills_processes_on_close() -> std::io::Result<bool> {
    if !current_process_is_in_job()? {
        return Ok(false);
    }

    let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
    // SAFETY: `limits` is writable and its exact buffer size is supplied.
    if unsafe {
        QueryInformationJobObject(
            null_mut(),
            JobObjectExtendedLimitInformation,
            &mut limits as *mut _ as *mut c_void,
            size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            null_mut(),
        )
    } == 0
    {
        return Err(std::io::Error::last_os_error());
    }
    Ok(limits.BasicLimitInformation.LimitFlags & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE != 0)
}

pub fn detach_server_daemon_command(command: &mut std::process::Command) {
    use std::os::windows::process::CommandExt;

    command.creation_flags(DETACHED_PROCESS);
}

pub fn current_process_is_detached_server_daemon() -> bool {
    if !unsafe { GetConsoleWindow() }.is_null() {
        return false;
    }

    matches!(current_process_is_in_job(), Ok(false))
}

pub(crate) fn available_pane_shell(child_pid: u32) -> Option<String> {
    available_pane_shell_from_snapshot(child_pid, &snapshot_processes())
}

fn available_pane_shell_from_snapshot(
    child_pid: u32,
    entries: &[WindowsProcessEntry],
) -> Option<String> {
    let shell = entries.iter().find(|entry| entry.pid == child_pid)?;
    if !super::is_pane_shell_process_name(&shell.name) {
        return None;
    }
    descendant_entries(child_pid, entries)
        .is_empty()
        .then(|| shell.name.clone())
}

pub fn foreground_group_leader_job(process_group_id: u32) -> Option<ForegroundJob> {
    let entries = cached_foreground_processes();
    let entry = entries.iter().find(|entry| entry.pid == process_group_id)?;
    Some(ForegroundJob {
        process_group_id,
        processes: vec![foreground_process_from_entry(entry)],
    })
}

pub fn foreground_process_group_id(child_pid: u32) -> Option<u32> {
    let entries = cached_foreground_processes();
    select_pane_foreground_job(child_pid, &entries).map(|job| job.process_group_id)
}

pub fn process_cwd(pid: u32) -> Option<PathBuf> {
    let process = ProcessHandle::open(pid, PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ)?;
    let process_parameters = read_process_parameters(process.0)?;
    read_unicode_string(process.0, process_parameters.current_directory.dos_path)
        .map(PathBuf::from)
        .filter(|path| path.is_absolute())
}

fn select_pane_foreground_job(
    shell_pid: u32,
    entries: &[WindowsProcessEntry],
) -> Option<ForegroundJob> {
    select_pane_foreground_job_with_runtime_inspection(
        shell_pid,
        entries,
        |shell| process_is_git_bash(shell.pid),
        |entry| process_runtime_marker(entry.pid),
    )
}

fn select_pane_foreground_job_with_runtime_inspection(
    shell_pid: u32,
    entries: &[WindowsProcessEntry],
    shell_is_git_bash: impl FnOnce(&WindowsProcessEntry) -> bool,
    mut runtime_marker: impl FnMut(&WindowsProcessEntry) -> Option<String>,
) -> Option<ForegroundJob> {
    let shell = entries.iter().find(|entry| entry.pid == shell_pid)?;
    let descendants = descendant_entries(shell_pid, entries);
    let mut candidates = Vec::new();
    for entry in std::iter::once(shell).chain(descendants) {
        if process_entry_identifies_agent(entry) {
            candidates.push(entry);
        }
    }

    if let Some(selected) = select_topmost_agent_chain_candidate(&candidates, entries) {
        return Some(foreground_job_from_entry(selected));
    }
    if !candidates.is_empty() || !shell_is_git_bash(shell) {
        return Some(foreground_job_from_entry(shell));
    }

    let escaped_candidates: Vec<_> = entries
        .iter()
        .filter(|entry| process_entry_identifies_agent(entry))
        .collect();
    if escaped_candidates.is_empty() {
        return Some(foreground_job_from_entry(shell));
    }

    let Some(shell_runtime_marker) = runtime_marker(shell).filter(|marker| !marker.is_empty())
    else {
        return Some(foreground_job_from_entry(shell));
    };
    let matching_candidates: Vec<_> = escaped_candidates
        .into_iter()
        .filter(|entry| runtime_marker(entry).as_deref() == Some(shell_runtime_marker.as_str()))
        .collect();
    let selected =
        select_topmost_agent_chain_candidate(&matching_candidates, entries).unwrap_or(shell);
    Some(foreground_job_from_entry(selected))
}

fn process_entry_identifies_agent(_entry: &WindowsProcessEntry) -> bool {
    false
}

fn foreground_job_from_entry(entry: &WindowsProcessEntry) -> ForegroundJob {
    ForegroundJob {
        process_group_id: entry.pid,
        processes: vec![foreground_process_from_entry(entry)],
    }
}

fn select_topmost_agent_chain_candidate<'a>(
    candidates: &[&'a WindowsProcessEntry],
    entries: &[WindowsProcessEntry],
) -> Option<&'a WindowsProcessEntry> {
    let parent_by_pid: HashMap<u32, u32> = entries
        .iter()
        .map(|entry| (entry.pid, entry.parent_pid))
        .collect();

    candidates.iter().copied().find(|entry| {
        candidates.iter().all(|other| {
            entry.pid == other.pid || process_is_ancestor(entry.pid, other.pid, &parent_by_pid)
        })
    })
}

fn process_is_ancestor(
    ancestor_pid: u32,
    descendant_pid: u32,
    parent_by_pid: &HashMap<u32, u32>,
) -> bool {
    let mut current = descendant_pid;
    let mut visited = HashSet::new();
    while visited.insert(current) {
        let Some(parent) = parent_by_pid.get(&current).copied() else {
            return false;
        };
        if parent == ancestor_pid {
            return true;
        }
        if parent == 0 {
            return false;
        }
        current = parent;
    }

    false
}

fn descendant_entries(root_pid: u32, entries: &[WindowsProcessEntry]) -> Vec<&WindowsProcessEntry> {
    let mut children: HashMap<u32, Vec<&WindowsProcessEntry>> = HashMap::new();
    for entry in entries {
        children.entry(entry.parent_pid).or_default().push(entry);
    }

    let mut output = Vec::new();
    let mut queue = VecDeque::new();
    let mut visited = HashSet::new();
    visited.insert(root_pid);
    if let Some(root_children) = children.get(&root_pid) {
        for entry in root_children.iter().copied() {
            if visited.insert(entry.pid) {
                queue.push_back(entry);
            }
        }
    }
    while let Some(entry) = queue.pop_front() {
        output.push(entry);
        if let Some(next) = children.get(&entry.pid) {
            for child in next.iter().copied() {
                if visited.insert(child.pid) {
                    queue.push_back(child);
                }
            }
        }
    }
    output
}

fn foreground_process_from_entry(entry: &WindowsProcessEntry) -> super::ForegroundProcess {
    super::ForegroundProcess {
        pid: entry.pid,
        name: entry.name.clone(),
        argv0: entry.argv0.clone(),
        argv: entry.argv.clone(),
        cmdline: entry.cmdline.clone(),
    }
}

fn snapshot_processes() -> Vec<WindowsProcessEntry> {
    let snapshot = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0) };
    if snapshot == INVALID_HANDLE_VALUE {
        return Vec::new();
    }
    let _snapshot = ProcessHandle(snapshot);

    let mut entry = PROCESSENTRY32W {
        dwSize: size_of::<PROCESSENTRY32W>() as u32,
        ..Default::default()
    };
    let mut output = Vec::new();
    let mut ok = unsafe { Process32FirstW(snapshot, &mut entry) } != 0;
    while ok {
        let pid = entry.th32ProcessID;
        let name = nul_terminated_utf16_to_string(&entry.szExeFile);
        let cmdline = process_command_line(pid);
        let argv = cmdline.as_deref().and_then(command_line_to_argv);
        let argv0 = argv
            .as_ref()
            .and_then(|argv| argv.first().cloned())
            .or_else(|| (!name.is_empty()).then(|| name.clone()));
        output.push(WindowsProcessEntry {
            pid,
            parent_pid: entry.th32ParentProcessID,
            name,
            argv0,
            argv,
            cmdline,
        });
        ok = unsafe { Process32NextW(snapshot, &mut entry) } != 0;
    }
    output
}

fn cached_foreground_processes() -> Arc<Vec<WindowsProcessEntry>> {
    let mut cache = FOREGROUND_PROCESS_SNAPSHOT_CACHE
        .lock()
        .unwrap_or_else(|err| err.into_inner());
    cache.snapshot(FOREGROUND_PROCESS_SNAPSHOT_CACHE_TTL, snapshot_processes)
}

impl ProcessSnapshotCache {
    fn snapshot(
        &mut self,
        max_age: Duration,
        build: impl FnOnce() -> Vec<WindowsProcessEntry>,
    ) -> Arc<Vec<WindowsProcessEntry>> {
        if let Some(cached) = &self.cached {
            if cached.built_at.elapsed() < max_age {
                return Arc::clone(&cached.entries);
            }
        }

        let entries = Arc::new(build());
        self.cached = Some(CachedProcessSnapshot {
            built_at: Instant::now(),
            entries: Arc::clone(&entries),
        });
        entries
    }
}

fn process_command_line(pid: u32) -> Option<String> {
    let process = ProcessHandle::open(pid, PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ)?;
    let parameters = read_process_parameters(process.0)?;
    read_unicode_string(process.0, parameters.command_line)
}

fn process_is_git_bash(pid: u32) -> bool {
    let Some(process) = ProcessHandle::open(pid, PROCESS_QUERY_LIMITED_INFORMATION) else {
        return false;
    };
    let Some(creation_time) = process_creation_time(process.0) else {
        return false;
    };
    {
        let mut cache = GIT_BASH_PROCESS_CACHE
            .lock()
            .unwrap_or_else(|err| err.into_inner());
        if let Some(cached) = cache.get_mut(&pid) {
            if cached.creation_time == creation_time {
                cached.last_used = Instant::now();
                return cached.is_git_bash;
            }
        }
    }

    let is_git_bash = process_executable_path(process.0)
        .as_deref()
        .is_some_and(|path| is_git_bash_executable_path(std::path::Path::new(path)));
    let mut cache = GIT_BASH_PROCESS_CACHE
        .lock()
        .unwrap_or_else(|err| err.into_inner());
    if cache.len() >= PROCESS_RUNTIME_MARKER_CACHE_CAPACITY {
        cache.retain(|_, cached| {
            cached.last_used.elapsed() < PROCESS_RUNTIME_MARKER_CACHE_RETENTION
        });
        if cache.len() >= PROCESS_RUNTIME_MARKER_CACHE_CAPACITY {
            cache.clear();
        }
    }
    cache.insert(
        pid,
        CachedGitBashProcess {
            creation_time,
            is_git_bash,
            last_used: Instant::now(),
        },
    );
    is_git_bash
}

fn process_executable_path(process: HANDLE) -> Option<String> {
    let mut path = vec![0_u16; 32_768];
    let mut len = path.len() as u32;
    if unsafe { QueryFullProcessImageNameW(process, 0, path.as_mut_ptr(), &mut len) } == 0 {
        return None;
    }
    String::from_utf16(&path[..len as usize]).ok()
}

fn command_uses_git_bash(command: &portable_pty::CommandBuilder) -> bool {
    let Some(program) = command.get_argv().first() else {
        return false;
    };
    let path = std::path::Path::new(program);
    if path.is_absolute() {
        return is_git_bash_executable_path(path);
    }
    if program.to_string_lossy().contains(['/', '\\']) {
        return false;
    }

    let Some(file_name) = path.file_name().and_then(OsStr::to_str) else {
        return false;
    };
    let candidate_name = if file_name.eq_ignore_ascii_case("bash") {
        "bash.exe"
    } else if file_name.eq_ignore_ascii_case("bash.exe") {
        file_name
    } else {
        return false;
    };
    let search_path = command
        .get_env("PATH")
        .map(OsStr::to_os_string)
        .or_else(|| std::env::var_os("PATH"));
    search_path.is_some_and(|search_path| {
        std::env::split_paths(&search_path)
            .map(|directory| directory.join(candidate_name))
            .find(|candidate| candidate.is_file())
            .is_some_and(|candidate| is_git_bash_executable_path(&candidate))
    })
}

fn is_git_bash_executable_path(path: &std::path::Path) -> bool {
    let Some(file_name) = path.file_name().and_then(OsStr::to_str) else {
        return false;
    };
    if !file_name.eq_ignore_ascii_case("bash.exe") || !path.is_absolute() || !path.is_file() {
        return false;
    }

    let Some(bin_dir) = path.parent() else {
        return false;
    };
    if !bin_dir
        .file_name()
        .and_then(OsStr::to_str)
        .is_some_and(|name| name.eq_ignore_ascii_case("bin"))
    {
        return false;
    }

    let Some(mut root) = bin_dir.parent() else {
        return false;
    };
    if root
        .file_name()
        .and_then(OsStr::to_str)
        .is_some_and(|name| name.eq_ignore_ascii_case("usr"))
    {
        let Some(parent) = root.parent() else {
            return false;
        };
        root = parent;
    }

    root.join("usr").join("bin").join("msys-2.0.dll").is_file()
        && root.join("cmd").join("git.exe").is_file()
}

fn process_runtime_marker(pid: u32) -> Option<String> {
    let process = ProcessHandle::open(pid, PROCESS_QUERY_INFORMATION | PROCESS_VM_READ)?;
    let creation_time = process_creation_time(process.0)?;
    {
        let mut cache = PROCESS_RUNTIME_MARKER_CACHE
            .lock()
            .unwrap_or_else(|err| err.into_inner());
        if let Some(cached) = cache.get_mut(&pid) {
            if cached.creation_time == creation_time
                && (cached.marker.is_some()
                    || cached.cached_at.elapsed() < PROCESS_RUNTIME_MARKER_NEGATIVE_TTL)
            {
                cached.last_used = Instant::now();
                return cached.marker.clone();
            }
        }
    }

    let marker = process_runtime_marker_from_handle(process.0)?;
    let mut cache = PROCESS_RUNTIME_MARKER_CACHE
        .lock()
        .unwrap_or_else(|err| err.into_inner());
    if cache.len() >= PROCESS_RUNTIME_MARKER_CACHE_CAPACITY {
        cache.retain(|_, cached| {
            cached.last_used.elapsed() < PROCESS_RUNTIME_MARKER_CACHE_RETENTION
        });
        if cache.len() >= PROCESS_RUNTIME_MARKER_CACHE_CAPACITY {
            cache.clear();
        }
    }
    cache.insert(
        pid,
        CachedProcessRuntimeMarker {
            creation_time,
            marker: marker.clone(),
            cached_at: Instant::now(),
            last_used: Instant::now(),
        },
    );
    marker
}

include!("windows_process.rs");

#[cfg(test)]
#[path = "windows/tests.rs"]
mod tests;
