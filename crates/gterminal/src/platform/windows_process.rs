fn process_creation_time(process: HANDLE) -> Option<u64> {
    let mut creation_time = FILETIME::default();
    let mut exit_time = FILETIME::default();
    let mut kernel_time = FILETIME::default();
    let mut user_time = FILETIME::default();
    if unsafe {
        GetProcessTimes(
            process,
            &mut creation_time,
            &mut exit_time,
            &mut kernel_time,
            &mut user_time,
        )
    } == 0
    {
        return None;
    }
    Some((u64::from(creation_time.dwHighDateTime) << 32) | u64::from(creation_time.dwLowDateTime))
}

fn process_runtime_marker_from_handle(process: HANDLE) -> Option<Option<String>> {
    let parameters = read_process_parameters(process)?;
    let environment = read_process_environment(process, parameters.environment)?;
    Some(environment_variable_from_utf16(
        &environment,
        PANE_RUNTIME_MARKER_ENV_VAR,
    ))
}

fn read_process_environment(process: HANDLE, address: *const c_void) -> Option<Vec<u16>> {
    if address.is_null() {
        return None;
    }

    let mut memory = MaybeUninit::<MEMORY_BASIC_INFORMATION>::uninit();
    let queried = unsafe {
        VirtualQueryEx(
            process,
            address,
            memory.as_mut_ptr(),
            size_of::<MEMORY_BASIC_INFORMATION>(),
        )
    };
    if queried == 0 {
        return None;
    }
    let memory = unsafe { memory.assume_init() };
    let address = address as usize;
    let base = memory.BaseAddress as usize;
    let offset = address.checked_sub(base)?;
    let available = memory.RegionSize.checked_sub(offset)?;
    let read_len = available.min(MAX_PROCESS_ENVIRONMENT_BYTES);
    if read_len < size_of::<u16>() {
        return None;
    }

    let max_units = read_len / size_of::<u16>();
    let chunk_units = PROCESS_ENVIRONMENT_READ_CHUNK_BYTES / size_of::<u16>();
    let mut environment = Vec::new();
    while environment.len() < max_units {
        let unit_count = (max_units - environment.len()).min(chunk_units);
        let chunk_bytes = unit_count * size_of::<u16>();
        let mut chunk = vec![0_u16; unit_count];
        let mut bytes_read = 0;
        let offset = environment.len().checked_mul(size_of::<u16>())?;
        let chunk_address = address.checked_add(offset)?;
        if unsafe {
            ReadProcessMemory(
                process,
                chunk_address as *const c_void,
                chunk.as_mut_ptr().cast::<c_void>(),
                chunk_bytes,
                &mut bytes_read,
            )
        } == 0
        {
            break;
        }
        chunk.truncate(bytes_read / size_of::<u16>());
        if chunk.is_empty() {
            break;
        }
        environment.extend_from_slice(&chunk);
        if let Some(end) = environment
            .windows(2)
            .position(|pair| pair == [0, 0])
            .map(|index| index + 2)
        {
            environment.truncate(end);
            return Some(environment);
        }
        if bytes_read < chunk_bytes {
            break;
        }
    }
    None
}

fn environment_variable_from_utf16(environment: &[u16], name: &str) -> Option<String> {
    for variable in environment.split(|unit| *unit == 0) {
        if variable.is_empty() {
            break;
        }
        let Some(separator) = variable.iter().position(|unit| *unit == u16::from(b'=')) else {
            continue;
        };
        let Ok(variable_name) = String::from_utf16(&variable[..separator]) else {
            continue;
        };
        if variable_name.eq_ignore_ascii_case(name) {
            return String::from_utf16(&variable[separator + 1..]).ok();
        }
    }
    None
}

fn read_process_parameters(process: HANDLE) -> Option<RtlUserProcessParameters> {
    let mut basic_info = MaybeUninit::<PROCESS_BASIC_INFORMATION>::uninit();
    let status = unsafe {
        NtQueryInformationProcess(
            process,
            ProcessBasicInformation,
            basic_info.as_mut_ptr().cast::<c_void>(),
            size_of::<PROCESS_BASIC_INFORMATION>() as u32,
            null_mut(),
        )
    };
    if status != STATUS_SUCCESS as NTSTATUS {
        return None;
    }

    let basic_info = unsafe { basic_info.assume_init() };
    if basic_info.PebBaseAddress.is_null() {
        return None;
    }

    let peb = read_process_value::<Peb>(process, basic_info.PebBaseAddress.cast::<c_void>())?;
    if peb.process_parameters.is_null() {
        return None;
    }

    read_process_value::<RtlUserProcessParameters>(process, peb.process_parameters.cast())
}

fn command_line_to_argv(command_line: &str) -> Option<Vec<String>> {
    let wide: Vec<u16> = command_line
        .encode_utf16()
        .chain(std::iter::once(0))
        .collect();
    let mut argc = 0;
    let argv_ptr = unsafe { CommandLineToArgvW(wide.as_ptr(), &mut argc) };
    if argv_ptr.is_null() || argc <= 0 {
        return None;
    }

    let argv_slice = unsafe { std::slice::from_raw_parts(argv_ptr, argc as usize) };
    let mut argv = Vec::with_capacity(argc as usize);
    for &arg in argv_slice {
        if arg.is_null() {
            continue;
        }
        let mut len = 0;
        unsafe {
            while *arg.add(len) != 0 {
                len += 1;
            }
            argv.push(String::from_utf16_lossy(std::slice::from_raw_parts(
                arg, len,
            )));
        }
    }
    unsafe {
        LocalFree(argv_ptr.cast());
    }
    Some(argv)
}

fn nul_terminated_utf16_to_string(buffer: &[u16]) -> String {
    let len = buffer
        .iter()
        .position(|&value| value == 0)
        .unwrap_or(buffer.len());
    String::from_utf16_lossy(&buffer[..len])
}

pub fn session_processes(child_pid: u32) -> Vec<u32> {
    if child_pid == 0 {
        return Vec::new();
    }

    let entries = snapshot_processes();
    session_processes_from_entries(child_pid, &entries)
}

fn session_processes_from_entries(child_pid: u32, entries: &[WindowsProcessEntry]) -> Vec<u32> {
    if !entries.iter().any(|entry| entry.pid == child_pid) {
        return Vec::new();
    }

    let mut pids = vec![child_pid];
    pids.extend(
        descendant_entries(child_pid, entries)
            .into_iter()
            .map(|entry| entry.pid),
    );
    pids
}

pub fn signal_processes(pids: &[u32], signal: Signal) {
    if signal == Signal::Hangup {
        return;
    }

    for &pid in pids {
        let Some(process) = ProcessHandle::open(pid, PROCESS_QUERY_LIMITED_INFORMATION) else {
            continue;
        };
        unsafe {
            TerminateProcess(process.0, 1);
        }
    }
}

pub fn process_exists(pid: u32) -> bool {
    let Some(process) = ProcessHandle::open(pid, PROCESS_QUERY_LIMITED_INFORMATION) else {
        return false;
    };

    let mut exit_code = 0;
    let ok = unsafe { GetExitCodeProcess(process.0, &mut exit_code) } != 0;
    ok && exit_code == STILL_ACTIVE
}

pub fn write_clipboard(bytes: &[u8]) -> bool {
    let Ok(text) = std::str::from_utf8(bytes) else {
        return false;
    };
    if text.contains('\0') {
        return false;
    }
    let mut utf16: Vec<u16> = text.encode_utf16().collect();
    utf16.push(0);
    let Some(byte_len) = utf16.len().checked_mul(size_of::<u16>()) else {
        return false;
    };

    unsafe {
        let owner = GetConsoleWindow();
        if owner.is_null() || OpenClipboard(owner) == 0 {
            return false;
        }
        let _clipboard = ClipboardGuard;

        if EmptyClipboard() == 0 {
            return false;
        }

        let memory = GlobalAlloc(GMEM_MOVEABLE, byte_len);
        if memory.is_null() {
            return false;
        }

        let locked = GlobalLock(memory);
        if locked.is_null() {
            GlobalFree(memory);
            return false;
        }
        copy_nonoverlapping(utf16.as_ptr(), locked.cast::<u16>(), utf16.len());
        GlobalUnlock(memory);

        if SetClipboardData(CF_UNICODETEXT as u32, memory).is_null() {
            GlobalFree(memory);
            return false;
        }

        true
    }
}

pub fn read_clipboard_text() -> Option<String> {
    None
}

pub fn open_url(url: &str) -> std::io::Result<()> {
    let operation = wide_null("open");
    let url = wide_null(url);
    let result = unsafe {
        ShellExecuteW(
            std::ptr::null_mut(),
            operation.as_ptr(),
            url.as_ptr(),
            std::ptr::null(),
            std::ptr::null(),
            1,
        )
    };
    if result as isize > 32 {
        Ok(())
    } else {
        Err(std::io::Error::other(format!(
            "failed to open URL with ShellExecuteW: code {}",
            result as isize
        )))
    }
}

// Windows does not wire clipboard-image bridging into semantic input yet.
#[cfg_attr(windows, allow(dead_code))]
pub fn read_clipboard_image() -> Option<ClipboardImage> {
    None
}

pub fn show_desktop_notification(title: &str, body: Option<&str>) -> std::io::Result<bool> {
    let title = title.to_owned();
    let body = body.unwrap_or(&title).to_owned();
    let (ready_tx, ready_rx) = std::sync::mpsc::sync_channel(1);
    std::thread::Builder::new()
        .name("gterm-windows-notification".into())
        .spawn(move || show_desktop_notification_on_thread(&title, &body, ready_tx))?;
    ready_rx
        .recv_timeout(Duration::from_secs(2))
        .map_err(|err| match err {
            std::sync::mpsc::RecvTimeoutError::Timeout => std::io::Error::new(
                std::io::ErrorKind::TimedOut,
                "Windows notification setup timed out",
            ),
            std::sync::mpsc::RecvTimeoutError::Disconnected => std::io::Error::other(
                "Windows notification thread exited before reporting readiness",
            ),
        })?
}

fn show_desktop_notification_on_thread(
    title: &str,
    body: &str,
    ready_tx: std::sync::mpsc::SyncSender<std::io::Result<bool>>,
) {
    let class_name = wide_null("STATIC");
    let window_name = wide_null("Gterm notifications");
    let hwnd = unsafe {
        CreateWindowExW(
            0,
            class_name.as_ptr(),
            window_name.as_ptr(),
            0,
            0,
            0,
            0,
            0,
            null_mut(),
            null_mut(),
            null_mut(),
            std::ptr::null(),
        )
    };
    if hwnd.is_null() {
        let _ = ready_tx.send(Err(std::io::Error::last_os_error()));
        return;
    }

    let mut notification = unsafe { std::mem::zeroed::<NOTIFYICONDATAW>() };
    notification.cbSize = size_of::<NOTIFYICONDATAW>() as u32;
    notification.hWnd = hwnd;
    notification.uID = 1;
    notification.hIcon = unsafe { LoadIconW(null_mut(), IDI_APPLICATION) };
    notification.uFlags = NIF_TIP;
    if !notification.hIcon.is_null() {
        notification.uFlags |= NIF_ICON;
    }
    copy_wide_truncated(&mut notification.szTip, "Gterm");

    if unsafe { Shell_NotifyIconW(NIM_ADD, &notification) } == 0 {
        let _ = ready_tx.send(Err(std::io::Error::other(
            "failed to add Gterm notification-area icon",
        )));
        unsafe {
            DestroyWindow(hwnd);
        }
        return;
    }

    notification.uFlags = NIF_INFO;
    notification.dwInfoFlags = NIIF_INFO | NIIF_NOSOUND;
    copy_wide_truncated(&mut notification.szInfoTitle, title);
    copy_wide_truncated(&mut notification.szInfo, body);
    if unsafe { Shell_NotifyIconW(NIM_MODIFY, &notification) } == 0 {
        unsafe {
            Shell_NotifyIconW(NIM_DELETE, &notification);
            DestroyWindow(hwnd);
        }
        let _ = ready_tx.send(Err(std::io::Error::other(
            "failed to show Gterm desktop notification",
        )));
        return;
    }

    let _ = ready_tx.send(Ok(true));
    std::thread::sleep(Duration::from_secs(10));
    unsafe {
        Shell_NotifyIconW(NIM_DELETE, &notification);
        DestroyWindow(hwnd);
    }
}

fn copy_wide_truncated<const N: usize>(destination: &mut [u16; N], value: &str) {
    destination.fill(0);
    let mut offset = 0;
    for ch in value.chars() {
        let mut units = [0; 2];
        let encoded = ch.encode_utf16(&mut units);
        if offset + encoded.len() >= N {
            break;
        }
        destination[offset..offset + encoded.len()].copy_from_slice(encoded);
        offset += encoded.len();
    }
}

fn wide_null(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(std::iter::once(0)).collect()
}

struct ProcessHandle(HANDLE);

struct ClipboardGuard;

impl Drop for ClipboardGuard {
    fn drop(&mut self) {
        unsafe {
            CloseClipboard();
        }
    }
}

impl ProcessHandle {
    fn open(pid: u32, access: u32) -> Option<Self> {
        if pid == 0 {
            return None;
        }
        let handle = unsafe { OpenProcess(access, 0, pid) };
        (!handle.is_null()).then_some(Self(handle))
    }
}

impl Drop for ProcessHandle {
    fn drop(&mut self) {
        unsafe {
            CloseHandle(self.0);
        }
    }
}

#[repr(C)]
#[derive(Clone, Copy)]
struct Peb {
    reserved1: [u8; 2],
    being_debugged: u8,
    reserved2: [u8; 1],
    reserved3: [*mut c_void; 2],
    ldr: *mut c_void,
    process_parameters: *mut RtlUserProcessParameters,
}

#[repr(C)]
#[derive(Clone, Copy)]
struct CurDir {
    dos_path: UNICODE_STRING,
    handle: HANDLE,
}

#[repr(C)]
#[derive(Clone, Copy)]
struct RtlUserProcessParameters {
    maximum_length: u32,
    length: u32,
    flags: u32,
    debug_flags: u32,
    console_handle: HANDLE,
    console_flags: u32,
    standard_input: HANDLE,
    standard_output: HANDLE,
    standard_error: HANDLE,
    current_directory: CurDir,
    dll_path: UNICODE_STRING,
    image_path_name: UNICODE_STRING,
    command_line: UNICODE_STRING,
    environment: *mut c_void,
}

fn read_process_value<T: Copy>(process: HANDLE, address: *const c_void) -> Option<T> {
    if address.is_null() {
        return None;
    }

    let mut value = MaybeUninit::<T>::uninit();
    let mut bytes_read = 0;
    let ok = unsafe {
        ReadProcessMemory(
            process,
            address,
            value.as_mut_ptr().cast::<c_void>(),
            size_of::<T>(),
            &mut bytes_read,
        )
    } != 0;

    (ok && bytes_read == size_of::<T>()).then(|| unsafe { value.assume_init() })
}

fn read_unicode_string(process: HANDLE, unicode: UNICODE_STRING) -> Option<String> {
    if unicode.Buffer.is_null() || unicode.Length == 0 || !unicode.Length.is_multiple_of(2) {
        return None;
    }

    let char_len = usize::from(unicode.Length / 2);
    let mut buffer = vec![0_u16; char_len];
    let mut bytes_read = 0;
    let ok = unsafe {
        ReadProcessMemory(
            process,
            unicode.Buffer.cast::<c_void>(),
            buffer.as_mut_ptr().cast::<c_void>(),
            usize::from(unicode.Length),
            &mut bytes_read,
        )
    } != 0;

    if !ok || bytes_read != usize::from(unicode.Length) {
        return None;
    }

    String::from_utf16(&buffer).ok()
}

// Prefix-mode ASCII input source support (see `switch_ascii_input_source_in_prefix`).
//
// Windows IMEs live in the terminal-emulator process, not in gterm. Empirically:
//   - `WM_IME_CONTROL` / `IMC_GETOPENSTATUS` reads whether the IME is open
//     (composing native characters) reliably across the process boundary (this
//     is what kren-select uses), so we detect state with it. The read goes
//     through `SendMessageTimeoutW` (`SMTO_ABORTIFHUNG`) so a hung host process
//     cannot block us indefinitely.
//   - Writing the state back (`IMC_SETOPENSTATUS` / `IMC_SETCONVERSIONMODE`)
//     changes the flag value but does NOT affect real input in terminal/TSF
//     hosts, so we cannot switch by writing the mode.
//   - `ImmGetContext` on the foreground window returns null across the process
//     boundary, so the ImmGetOpenStatus/ImmSetOpenStatus path is unavailable.
// Therefore we switch the way kren-select does: inject the IME toggle key with
// `SendInput`, which reaches the foreground input queue like a real keypress.
//
// The toggle key is language-specific, so we pick it from the foreground
// keyboard layout's language id. Only Korean is mapped today; other IMEs are
// detected and left untouched (a no-op) rather than toggled with the wrong key.

/// `WM_IME_CONTROL` sub-command that reads whether the IME is open, i.e.
/// composing native characters. This is `IMC_GETOPENSTATUS` (0x0005); for the
/// Korean IME "open" is exactly the Hangul state and "closed" is English/ASCII
/// direct input, which is the state we detect and toggle.
const IMC_GETOPENSTATUS: usize = 0x0005;

/// Virtual key that toggles Hangul/English on Korean IMEs.
const VK_HANGUL: u16 = 0x15;

/// Primary language id (low 10 bits of a LANGID) for Korean.
const LANG_KOREAN: u32 = 0x12;

/// Whether the IME reports itself open, i.e. composing native characters
/// (Hangul for the Korean IME). `IMC_GETOPENSTATUS` returns nonzero when the
/// IME is open and zero when it is in direct English/ASCII input.
fn ime_open(open_status: isize) -> bool {
    open_status != 0
}

/// Timeout (ms) for the cross-process IME open-status read. Short enough that a
/// hung terminal never freezes prefix-mode entry/exit.
const IME_STATUS_READ_TIMEOUT_MS: u32 = 200;

/// Reads the IME open status (`IMC_GETOPENSTATUS`) with a bounded timeout.
///
/// `WM_IME_CONTROL` crosses into the terminal-emulator process, and a plain
/// `SendMessageW` would block gterm's client thread until that process responds
/// (indefinitely if it is hung). `SendMessageTimeoutW` with `SMTO_ABORTIFHUNG`
/// caps the wait; on timeout or failure this returns `None` and callers leave
/// the IME untouched rather than blocking or guessing.
fn read_ime_open_status(ime_hwnd: HWND) -> Option<isize> {
    let mut result: usize = 0;
    // SAFETY: `ime_hwnd` is a non-null IME window from `ImmGetDefaultIMEWnd`, and
    // `result` is a valid out-pointer for the message's `DWORD_PTR` result.
    let ret = unsafe {
        SendMessageTimeoutW(
            ime_hwnd,
            WM_IME_CONTROL,
            IMC_GETOPENSTATUS,
            0,
            SMTO_ABORTIFHUNG,
            IME_STATUS_READ_TIMEOUT_MS,
            &mut result,
        )
    };
    if ret == 0 {
        // Timed out or failed; do not block or assume a state.
        return None;
    }
    Some(result as isize)
}

/// The IME toggle key for a keyboard layout language id, or `None` when the
/// language's toggle key is not known. `langid` is the full LANGID (LOWORD of
/// an `HKL`); the primary language is its low 10 bits.
///
/// Only Korean is mapped: `VK_HANGUL` is the Hangul/English toggle. Japanese
/// (half/full-width) and Chinese use different keys per IME, so they return
/// `None` and are left untouched instead of toggled incorrectly.
fn toggle_key_for_language(langid: u32) -> Option<u16> {
    match langid & 0x3FF {
        LANG_KOREAN => Some(VK_HANGUL),
        _ => None,
    }
}

/// Builds the key-down then key-up `INPUT` pair for `vk`.
fn key_tap_inputs(vk: u16) -> [INPUT; 2] {
    let key_event = |flags| INPUT {
        r#type: INPUT_KEYBOARD,
        Anonymous: INPUT_0 {
            ki: KEYBDINPUT {
                wVk: vk,
                wScan: 0,
                dwFlags: flags,
                time: 0,
                dwExtraInfo: 0,
            },
        },
    };
    [key_event(0), key_event(KEYEVENTF_KEYUP)]
}

/// Injects a key-down then key-up for `vk` via `SendInput`.
///
/// Returns `true` when the key-down was queued and the IME may have toggled.
/// Thin wrapper over [`send_vk_tap_with`] that plugs in the real `SendInput`;
/// the injection policy lives there so it can be unit-tested without the OS.
fn send_vk_tap(vk: u16) -> bool {
    send_vk_tap_with(vk, |events| {
        // SAFETY: `events` outlives the call; its `INPUT_KEYBOARD` entries have
        // the `ki` union variant fully initialized, which is the variant
        // SendInput reads for keyboard input. `size_of::<INPUT>()` is the
        // required `cbSize`.
        unsafe {
            SendInput(
                events.len() as u32,
                events.as_ptr(),
                size_of::<INPUT>() as i32,
            )
        }
    })
}

/// Core key-tap logic with the raw event injector abstracted behind `inject`,
/// which returns how many of the passed events it actually queued. This keeps
/// the success / partial-injection / total-failure branches unit-testable
/// without touching the real `SendInput`.
///
/// `SendInput` returns how many events it queued; a short count means injection
/// was blocked (e.g. by UIPI). Returns `true` whenever the key-down was queued,
/// because the IME may have toggled and callers must retain restoration state.
/// When only the key-down landed, the key-up is retried so the key is not left
/// logically held down.
fn send_vk_tap_with(vk: u16, mut inject: impl FnMut(&[INPUT]) -> u32) -> bool {
    let inputs = key_tap_inputs(vk);
    let sent = inject(&inputs);
    if sent as usize == inputs.len() {
        return true;
    }

    if sent == 1 {
        // The key-down landed and may already have toggled the IME. Retry the
        // dropped key-up, but report that restoration state is still required.
        let key_up = [inputs[1]];
        let up_sent = inject(&key_up);
        tracing::warn!(
            vk,
            sent,
            expected = inputs.len(),
            key_up_retry_sent = up_sent,
            "SendInput dropped the IME toggle key-up; retried key-up"
        );
        return true;
    }

    tracing::warn!(
        vk,
        sent,
        expected = inputs.len(),
        "SendInput did not inject the IME toggle key tap"
    );
    false
}

pub(crate) fn pump_input_source_runloop() {}

/// Switch the foreground window's IME to ASCII-capable input for prefix mode.
///
/// Returns `None` (nothing to restore) when there is no foreground IME, the
/// keyboard language has no known toggle key, or the IME is already
/// ASCII-capable, matching the macOS contract.
pub(crate) fn switch_to_ascii_input_source() -> Option<InputSourceRestore> {
    // SAFETY: all calls are Win32 UI functions invoked on the client's main
    // thread. Every HWND is null-checked before use; `fg_thread` is a thread id
    // (not a handle) used only as `GetKeyboardLayout` input, where 0 harmlessly
    // falls back to the calling thread's layout.
    unsafe {
        let fg = GetForegroundWindow();
        if fg.is_null() {
            return None;
        }

        // Pick the toggle key for the foreground keyboard language. Unknown
        // languages (Japanese, Chinese, ...) are left untouched.
        let fg_thread = GetWindowThreadProcessId(fg, null_mut());
        let langid = (GetKeyboardLayout(fg_thread) as usize as u32) & 0xFFFF;
        let Some(toggle_vk) = toggle_key_for_language(langid) else {
            tracing::debug!(
                langid = format!("{langid:#06x}"),
                "prefix IME switch: no toggle key for keyboard language, leaving IME as-is"
            );
            return None;
        };

        // Detect the open (Hangul) state via the bounded read path.
        let ime_hwnd = ImmGetDefaultIMEWnd(fg);
        if ime_hwnd.is_null() {
            return None;
        }
        let Some(open) = read_ime_open_status(ime_hwnd) else {
            tracing::debug!("prefix IME switch skipped: IME open-status read timed out");
            return None;
        };
        if !ime_open(open) {
            // Already in English/ASCII input; nothing to switch or restore.
            return None;
        }

        // The bounded cross-process status read can take long enough for focus
        // to change. Recheck immediately before using the global input queue.
        if GetForegroundWindow() != fg {
            tracing::debug!("prefix IME switch skipped: foreground window changed");
            return None;
        }

        // Toggle to ASCII by injecting the language's IME toggle key. Only arm
        // restoration when the toggle actually landed, so we never try to
        // restore a switch that never happened.
        if !send_vk_tap(toggle_vk) {
            tracing::warn!(
                langid = format!("{langid:#06x}"),
                "prefix IME switch: toggle injection failed, leaving IME as-is"
            );
            return None;
        }
        tracing::debug!(
            langid = format!("{langid:#06x}"),
            "switched host IME to ASCII for prefix mode"
        );
        Some(InputSourceRestore {
            toggle_vk,
            origin_hwnd: fg as isize,
        })
    }
}

/// Restores the native (Hangul) IME state that was active before prefix mode.
///
/// Only constructed by [`switch_to_ascii_input_source`] after it successfully
/// toggled the IME to English/ASCII. Dropping it re-injects the same toggle key
/// to go back, but only after two guards, so restoration never fights the user
/// or another application:
///   - the same window that was switched must still be focused, otherwise the
///     toggle would land on whatever app the user moved to;
///   - the IME must still be in English (our switch still in effect), otherwise
///     the user manually returned to Hangul during prefix mode and we must leave
///     their choice alone.
///
/// `origin_hwnd` stores the foreground window at switch time as raw pointer bits
/// (`isize`, not `HWND`) so the guard stays `Send` when parked in the client's
/// prefix-input state across `.await` points.
#[derive(Debug)]
pub(crate) struct InputSourceRestore {
    toggle_vk: u16,
    origin_hwnd: isize,
}

impl Drop for InputSourceRestore {
    fn drop(&mut self) {
        // SAFETY: all calls are Win32 UI functions invoked on the client's main
        // thread. Every HWND is null-checked before use.
        unsafe {
            // Guard 1: only restore if the window we switched is still focused,
            // so the toggle never lands on a different application.
            let fg = GetForegroundWindow();
            if fg.is_null() || fg as isize != self.origin_hwnd {
                tracing::debug!(
                    "prefix IME restore skipped: foreground window changed since switch"
                );
                return;
            }

            // Guard 2: only restore if the IME is still in English (our switch is
            // still in effect). If the user manually switched back to Hangul
            // during prefix mode, leave their choice untouched.
            let ime_hwnd = ImmGetDefaultIMEWnd(fg);
            if ime_hwnd.is_null() {
                return;
            }
            let Some(open) = read_ime_open_status(ime_hwnd) else {
                tracing::debug!("prefix IME restore skipped: IME open-status read timed out");
                return;
            };
            if ime_open(open) {
                tracing::debug!("prefix IME restore skipped: IME already back to native input");
                return;
            }

            // The bounded cross-process status read can take long enough for
            // focus to change. Recheck immediately before using SendInput.
            if GetForegroundWindow() != fg {
                tracing::debug!("prefix IME restore skipped: foreground window changed");
                return;
            }

            if send_vk_tap(self.toggle_vk) {
                tracing::debug!("restored host IME after prefix mode");
            }
        }
    }
}
