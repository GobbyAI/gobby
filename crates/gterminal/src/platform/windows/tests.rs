use std::{
    fs,
    process::{Command, Stdio},
    sync::Arc,
    thread,
    time::{Duration, Instant},
};

use windows_sys::Win32::System::Console::{
    AllocConsole, FreeConsole, GetConsoleProcessList, GetConsoleWindow,
};

#[test]
fn windows_conpty_native_encoder_uses_canonical_phase_and_repeat_count() {
    let key = crate::input::TerminalKey::new(
        crossterm::event::KeyCode::Esc,
        crossterm::event::KeyModifiers::empty(),
    )
    .with_windows_record(crate::input::WindowsKeyRecord {
        key_down: true,
        repeat_count: 3,
        virtual_key_code: 27,
        virtual_scan_code: 1,
        unicode: 27,
        control_key_state: 0,
    });

    assert_eq!(
        super::encode_windows_conpty_fallback(&key),
        Some(b"\x1b[27;1;27;1;0;3_".to_vec())
    );
    let mut release = key.with_kind(crossterm::event::KeyEventKind::Release);
    release.repeat_count = 3;
    assert_eq!(
        super::encode_windows_conpty_fallback(&release),
        Some(b"\x1b[27;1;27;0;0;1_".to_vec())
    );
}

#[test]
fn windows_conpty_native_encoder_preserves_semantic_escape_fallback() {
    let escape = crate::input::TerminalKey::new(
        crossterm::event::KeyCode::Esc,
        crossterm::event::KeyModifiers::empty(),
    );

    assert_eq!(
        super::encode_windows_conpty_fallback(&escape),
        Some(b"\x1b[27;1;27;1;0;1_\x1b[27;1;27;0;0;1_".to_vec())
    );
    assert_eq!(
        super::encode_windows_conpty_fallback(
            &escape
                .clone()
                .with_kind(crossterm::event::KeyEventKind::Repeat),
        ),
        None
    );
    assert_eq!(
        super::encode_windows_conpty_fallback(
            &escape
                .clone()
                .with_kind(crossterm::event::KeyEventKind::Release),
        ),
        None
    );
    assert_eq!(
        super::encode_windows_conpty_fallback(&escape.clone().with_vt_bytes(vec![27])),
        None
    );
    assert_eq!(
        super::encode_windows_conpty_fallback(&crate::input::TerminalKey::new(
            crossterm::event::KeyCode::Esc,
            crossterm::event::KeyModifiers::ALT,
        ),),
        None
    );
}

#[test]
fn windows_conpty_native_encoder_preserves_semantic_shift_enter_fallback() {
    let shift_enter = crate::input::TerminalKey::new(
        crossterm::event::KeyCode::Enter,
        crossterm::event::KeyModifiers::SHIFT,
    );

    assert_eq!(
        super::encode_windows_conpty_fallback(&shift_enter),
        Some(b"\x1b[13;28;13;1;16;1_".to_vec())
    );
    assert_eq!(
        super::encode_windows_conpty_fallback(&crate::input::TerminalKey::new(
            crossterm::event::KeyCode::Enter,
            crossterm::event::KeyModifiers::empty(),
        )),
        None
    );
}

#[test]
fn windows_notification_text_is_null_terminated_and_unicode_safe() {
    let mut destination = [u16::MAX; 6];
    super::copy_wide_truncated(&mut destination, "abc😀def");

    assert_eq!(String::from_utf16(&destination[..5]).unwrap(), "abc😀");
    assert_eq!(destination[5], 0);
}

#[test]
fn powershell_agent_command_omits_argument_list_when_no_arguments_are_passed() {
    let argv = vec!["opencode".into()];

    assert_eq!(
        super::interactive_shell_command(&argv, "powershell.exe").as_deref(),
        Some("& opencode")
    );
}

#[test]
fn cmd_agent_command_encodes_edge_arguments_without_cmd_expansion() {
    use base64::Engine as _;

    assert_eq!(super::super::quote_powershell_arg("@options"), "'@options'");
    let argv = vec![
        "pi".into(),
        String::new(),
        "two words".into(),
        "100%".into(),
        "wow!".into(),
        "a'b".into(),
    ];
    let command = super::interactive_shell_command(&argv, "cmd.exe").unwrap();
    let encoded = command.split_whitespace().last().unwrap();
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(encoded)
        .unwrap();
    let utf16 = bytes
        .chunks_exact(2)
        .map(|chunk| u16::from_le_bytes([chunk[0], chunk[1]]))
        .collect::<Vec<_>>();
    assert_eq!(
            String::from_utf16(&utf16).unwrap(),
            "$p=Start-Process -FilePath pi -ArgumentList '\"\" \"two words\" 100% wow! a''b' -NoNewWindow -Wait -PassThru"
        );
}

#[test]
fn windows_shells_round_trip_agent_arguments_through_a_real_command() {
    let base = std::env::temp_dir().join(format!(
        "gterm-agent-argv-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_millis()
    ));
    fs::create_dir_all(&base).unwrap();
    let helper = base.join("pi.cmd");
    fs::write(
            &helper,
            "@echo off\r\n>\"%GTERM_ARGV_CAPTURE%\" (\r\necho(%~1\r\necho(%~2\r\necho(%~3\r\necho(%~4\r\necho(%~5\r\necho(%~6\r\n)\r\n",
        )
        .unwrap();
    let argv = vec![
        "pi".into(),
        String::new(),
        "two words".into(),
        "100%".into(),
        "wow!".into(),
        "a'b".into(),
        "@options".into(),
    ];
    let inherited_path = std::env::var_os("PATH").unwrap_or_default();
    let path = format!("{};{}", base.display(), inherited_path.to_string_lossy());
    let run_command = |shell: &str, command: &str, capture: &std::path::Path| {
        let mut process = if shell == "cmd.exe" {
            let mut process = Command::new("cmd.exe");
            process.args(["/d", "/c", command]);
            process
        } else {
            let mut process = Command::new("powershell.exe");
            process.args(["-NoLogo", "-NoProfile", "-Command", command]);
            process
        };
        process
            .env("PATH", &path)
            .env("GTERM_ARGV_CAPTURE", capture)
            .status()
            .unwrap()
    };

    for shell in ["powershell.exe", "cmd.exe"] {
        let no_args_capture = base.join(format!("{shell}-no-args.txt"));
        let no_args_command = super::interactive_shell_command(&["pi".into()], shell).unwrap();
        let status = run_command(shell, &no_args_command, &no_args_capture);
        assert!(status.success(), "{shell} argument-free command failed");
        assert_eq!(
            fs::read_to_string(no_args_capture)
                .unwrap()
                .replace("\r\n", "\n"),
            "\n\n\n\n\n\n"
        );

        let capture = base.join(format!("{shell}.txt"));
        let command = super::interactive_shell_command(&argv, shell).unwrap();
        let status = run_command(shell, &command, &capture);
        assert!(status.success(), "{shell} command failed");
        assert_eq!(
            fs::read_to_string(capture).unwrap().replace("\r\n", "\n"),
            "\ntwo words\n100%\nwow!\na'b\n@options\n"
        );
    }

    let _ = fs::remove_dir_all(base);
}

const CONSOLE_TEST_CHILD_ENV: &str = "GTERM_TEST_CONSOLE_CHILD_MODE";
const CONSOLE_TEST_PARENT_PID_ENV: &str = "GTERM_TEST_CONSOLE_PARENT_PID";
const WMI_DAEMON_TEST_CHILD_ENV: &str = "GTERM_TEST_WMI_DAEMON_CHILD";

#[test]
fn windows_environment_keys_use_unicode_case_insensitive_ordering() {
    assert_eq!(
        super::windows_environment_key_cmp("hérdr", "HÉRDR"),
        std::cmp::Ordering::Equal
    );
}

#[test]
fn windows_wmi_daemon_preserves_environment_and_working_directory() {
    if let Some(capture) = std::env::var_os(WMI_DAEMON_TEST_CHILD_ENV) {
        let cwd = std::env::current_dir().expect("WMI daemon test working directory");
        fs::write(
            capture,
            format!(
                "{}\n{}",
                cwd.display(),
                super::current_process_is_detached_server_daemon()
            ),
        )
        .expect("write WMI daemon test capture");
        return;
    }

    let base = std::env::temp_dir().join(format!(
        "gterm-wmi-daemon-test-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_millis()
    ));
    fs::create_dir_all(&base).unwrap();
    let capture = base.join("capture.txt");
    let test_exe = std::env::current_exe().expect("resolve test executable");
    let mut child = Command::new(test_exe);
    child
        .arg("windows_wmi_daemon_preserves_environment_and_working_directory")
        .current_dir(&base)
        .env(WMI_DAEMON_TEST_CHILD_ENV, &capture)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());

    let pid =
        super::launch_server_daemon_with_wmi(&child).expect("launch detached process through WMI");
    assert_ne!(pid, 0, "WMI returned an invalid process id");

    let expected = format!("{}\ntrue", base.display());
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if fs::read_to_string(&capture).is_ok_and(|captured| captured == expected) {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "WMI daemon child did not write the expected capture"
        );
        thread::sleep(Duration::from_millis(50));
    }
    let _ = fs::remove_dir_all(base);
}

fn console_process_ids() -> Vec<u32> {
    let mut process_ids = vec![0; 8];
    loop {
        let count =
            unsafe { GetConsoleProcessList(process_ids.as_mut_ptr(), process_ids.len() as u32) }
                as usize;
        if count == 0 {
            return Vec::new();
        }
        if count <= process_ids.len() {
            process_ids.truncate(count);
            return process_ids;
        }
        process_ids.resize(count, 0);
    }
}

#[test]
fn windows_background_and_server_daemon_commands_do_not_have_consoles() {
    if let Some(mode) = std::env::var_os(CONSOLE_TEST_CHILD_ENV) {
        assert!(
            unsafe { GetConsoleWindow() }.is_null(),
            "{} child opened or inherited a console window",
            mode.to_string_lossy()
        );
        let parent_pid = std::env::var(CONSOLE_TEST_PARENT_PID_ENV)
            .expect("console test parent pid")
            .parse::<u32>()
            .expect("numeric console test parent pid");
        assert!(
            !console_process_ids().contains(&parent_pid),
            "{} child inherited the parent console",
            mode.to_string_lossy()
        );
        return;
    }

    let allocated_console = if console_process_ids().is_empty() {
        assert_ne!(unsafe { AllocConsole() }, 0, "allocate test console");
        true
    } else {
        false
    };

    let parent_pid = std::process::id().to_string();
    let test_exe = std::env::current_exe().expect("resolve test executable");
    let configurations: [(&str, fn(&mut Command)); 2] = [
        ("background", super::configure_background_command_platform),
        ("server daemon", super::detach_server_daemon_command),
    ];
    for (mode, configure) in configurations {
        let mut child = Command::new(&test_exe);
        child
            .arg("windows_background_and_server_daemon_commands_do_not_have_consoles")
            .env(CONSOLE_TEST_CHILD_ENV, mode)
            .env(CONSOLE_TEST_PARENT_PID_ENV, &parent_pid)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        configure(&mut child);

        let status = child.status().expect("spawn console isolation test child");
        assert!(
            status.success(),
            "{mode} child opened or inherited a console"
        );
    }

    let command = format!(
        r#""{}" windows_background_and_server_daemon_commands_do_not_have_consoles"#,
        test_exe.display()
    );
    let status = crate::platform::detached_custom_command_process(&command)
        .env(CONSOLE_TEST_CHILD_ENV, "detached custom command descendant")
        .env(CONSOLE_TEST_PARENT_PID_ENV, &parent_pid)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .expect("spawn detached custom command test child");
    assert!(
        status.success(),
        "detached custom command descendant opened or inherited a console"
    );

    if allocated_console {
        unsafe {
            FreeConsole();
        }
    }
}

fn argv_strings(argv: &[std::ffi::OsString]) -> Vec<String> {
    argv.into_iter()
        .map(|arg| arg.to_string_lossy().into_owned())
        .collect()
}

#[test]
fn pane_custom_command_uses_cmd() {
    let builder = super::pane_custom_command_pty_builder_with_comspec(
        "echo hello",
        Some(r"C:\Windows\System32\cmd.exe".into()),
    );

    assert_eq!(
        argv_strings(builder.get_argv()),
        [r"C:\Windows\System32\cmd.exe", "/d", "/c"]
    );
}

#[test]
fn detached_custom_command_uses_cmd() {
    let expected_shell = std::env::var_os("ComSpec")
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| r"C:\Windows\System32\cmd.exe".into())
        .to_string_lossy()
        .into_owned();

    let process = super::detached_custom_command_process_platform("echo hello");

    assert_eq!(process.get_program().to_string_lossy(), expected_shell);
    assert_eq!(
        process
            .get_args()
            .map(|arg| arg.to_string_lossy().into_owned())
            .collect::<Vec<_>>(),
        ["/d", "/c", "echo hello"]
    );
}

#[test]
fn custom_command_falls_back_when_comspec_is_empty() {
    let builder =
        super::pane_custom_command_pty_builder_with_comspec("echo hello", Some("".into()));

    assert_eq!(
        argv_strings(builder.get_argv()),
        [r"C:\Windows\System32\cmd.exe", "/d", "/c"]
    );
}

#[test]
fn detached_custom_command_preserves_quoted_command_tail() {
    let path = std::env::temp_dir().join(format!(
        "gterm-raw-command-quotes-{}.txt",
        std::process::id()
    ));
    let command = format!(r#"echo "hi" > "{}""#, path.display());

    let status = super::detached_custom_command_process_platform(&command)
        .status()
        .expect("spawn raw command");

    assert!(status.success(), "{status:?}");
    let content = std::fs::read_to_string(&path).expect("read command output");
    let _ = std::fs::remove_file(&path);
    assert!(content.contains(r#""hi""#), "{content:?}");
    assert!(!content.contains(r#"\"hi\""#), "{content:?}");
}

#[test]
fn windows_process_cwd_reads_child_launch_directory() {
    let cwd = std::env::temp_dir().join(format!("gterm-cwd-test-{}", std::process::id()));
    fs::create_dir_all(&cwd).expect("create cwd fixture");

    let shell =
        std::env::var_os("ComSpec").unwrap_or_else(|| r"C:\Windows\System32\cmd.exe".into());
    let mut child = Command::new(shell)
        .args(["/D", "/Q", "/C", "ping -n 11 127.0.0.1 > NUL"])
        .current_dir(&cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("spawn cmd");

    let deadline = Instant::now() + Duration::from_secs(5);
    let mut observed = None;
    while Instant::now() < deadline {
        observed = super::process_cwd(child.id());
        if observed.as_deref() == Some(cwd.as_path()) {
            break;
        }
        thread::sleep(Duration::from_millis(100));
    }

    let _ = child.kill();
    let _ = child.wait();
    let _ = fs::remove_dir_all(&cwd);

    assert_eq!(observed.as_deref(), Some(cwd.as_path()));
}

#[test]
fn windows_process_environment_reads_runtime_marker() {
    let shell =
        std::env::var_os("ComSpec").unwrap_or_else(|| r"C:\Windows\System32\cmd.exe".into());
    let mut child = Command::new(shell)
        .args(["/D", "/Q", "/C", "ping -n 11 127.0.0.1 > NUL"])
        .env(super::PANE_RUNTIME_MARKER_ENV_VAR, "pane-test")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("spawn cmd");

    let deadline = Instant::now() + Duration::from_secs(5);
    let mut observed = None;
    while Instant::now() < deadline {
        observed = super::process_runtime_marker(child.id());
        if observed.as_deref() == Some("pane-test") {
            break;
        }
        thread::sleep(Duration::from_millis(100));
    }

    let _ = child.kill();
    let _ = child.wait();

    assert_eq!(observed.as_deref(), Some("pane-test"));
}

#[test]
fn windows_process_tree_selects_direct_agent_descendant() {
    let entries = vec![
        test_entry(10, 1, "powershell.exe", &["powershell.exe"]),
        test_entry(20, 10, "codex.exe", &["codex.exe"]),
    ];

    let job = super::select_pane_foreground_job_with_runtime_inspection(
        10,
        &entries,
        |_| panic!("Git Bash fallback must not run after normal detection succeeds"),
        |_| panic!("runtime marker must not be read after normal detection succeeds"),
    )
    .unwrap();

    assert_eq!(job.process_group_id, 20);
    assert_eq!(job.processes.len(), 1);
    assert_eq!(job.processes[0].name, "codex.exe");
}

#[test]
fn windows_process_tree_recovers_git_bash_exec_chain_from_runtime_marker() {
    let entries = vec![
        test_entry(10, 1, "bash.exe", &[r"C:\Program Files\Git\bin\bash.exe"]),
        test_entry(
            11,
            10,
            "bash.exe",
            &[r"C:\Program Files\Git\usr\bin\bash.exe"],
        ),
        test_entry(
            20,
            99,
            "sh.exe",
            &[r"C:\Program Files\Git\usr\bin\sh.exe", "/c/npm/codex"],
        ),
        test_entry(
            30,
            20,
            "node.exe",
            &[
                r"C:\Program Files\nodejs\node.exe",
                r"C:\Users\user\AppData\Roaming\npm\node_modules\@openai\codex\bin\codex.js",
            ],
        ),
        test_entry(
            40,
            30,
            "codex.exe",
            &[r"C:\npm\node_modules\@openai\codex\bin\codex.exe"],
        ),
    ];
    let mut inspected = Vec::new();

    let job = super::select_pane_foreground_job_with_runtime_inspection(
        10,
        &entries,
        |_| true,
        |entry| {
            inspected.push(entry.pid);
            Some("pane-a".to_string())
        },
    )
    .unwrap();

    assert_eq!(job.process_group_id, 20);
    assert_eq!(job.processes[0].name, "sh.exe");
    assert_eq!(inspected, vec![10, 20, 30, 40]);
}

#[test]
fn windows_process_tree_skips_runtime_inspection_for_non_git_bash_shell() {
    let entries = vec![
        test_entry(10, 1, "powershell.exe", &["powershell.exe"]),
        test_entry(20, 99, "codex.exe", &["codex.exe"]),
    ];

    let job = super::select_pane_foreground_job_with_runtime_inspection(
        10,
        &entries,
        |_| false,
        |_| panic!("runtime marker must not be read for non-Git-Bash panes"),
    )
    .unwrap();

    assert_eq!(job.process_group_id, 10);
}

#[test]
fn windows_process_tree_skips_runtime_inspection_without_agent_candidate() {
    let entries = vec![
        test_entry(10, 1, "bash.exe", &[r"C:\Program Files\Git\bin\bash.exe"]),
        test_entry(20, 99, "git.exe", &["git.exe", "status"]),
    ];

    let job = super::select_pane_foreground_job_with_runtime_inspection(
        10,
        &entries,
        |_| true,
        |_| panic!("runtime marker must not be read without an agent candidate"),
    )
    .unwrap();

    assert_eq!(job.process_group_id, 10);
}

#[test]
fn windows_process_tree_rejects_missing_or_empty_shell_runtime_marker() {
    let entries = vec![
        test_entry(10, 1, "bash.exe", &[r"C:\Program Files\Git\bin\bash.exe"]),
        test_entry(20, 99, "codex.exe", &["codex.exe"]),
    ];

    for shell_marker in [None, Some(String::new())] {
        let job = super::select_pane_foreground_job_with_runtime_inspection(
            10,
            &entries,
            |_| true,
            |entry| {
                if entry.pid == 10 {
                    shell_marker.clone()
                } else {
                    Some("pane-a".to_string())
                }
            },
        )
        .unwrap();

        assert_eq!(job.process_group_id, 10);
    }
}

#[test]
fn windows_process_tree_rejects_runtime_marker_from_another_pane() {
    let entries = vec![
        test_entry(10, 1, "bash.exe", &[r"C:\Program Files\Git\bin\bash.exe"]),
        test_entry(20, 99, "codex.exe", &["codex.exe"]),
    ];

    let job = super::select_pane_foreground_job_with_runtime_inspection(
        10,
        &entries,
        |_| true,
        |entry| Some(if entry.pid == 10 { "pane-a" } else { "pane-b" }.to_string()),
    )
    .unwrap();

    assert_eq!(job.process_group_id, 10);
    assert_eq!(job.processes[0].name, "bash.exe");
}

#[test]
fn windows_process_tree_rejects_ambiguous_runtime_marker_candidates() {
    let entries = vec![
        test_entry(10, 1, "bash.exe", &[r"C:\Program Files\Git\bin\bash.exe"]),
        test_entry(20, 99, "codex.exe", &["codex.exe"]),
        test_entry(30, 98, "claude.exe", &["claude.exe"]),
    ];

    let job = super::select_pane_foreground_job_with_runtime_inspection(
        10,
        &entries,
        |_| true,
        |_| Some("pane-a".to_string()),
    )
    .unwrap();

    assert_eq!(job.process_group_id, 10);
    assert_eq!(job.processes[0].name, "bash.exe");
}

#[test]
fn windows_foreground_process_snapshot_is_shared_within_ttl() {
    let mut cache = super::ProcessSnapshotCache { cached: None };
    let mut builds = 0;
    let mut first_build_completed_at = None;

    let first = cache.snapshot(Duration::from_secs(60), || {
        builds += 1;
        let entries = vec![test_entry(10, 1, "powershell.exe", &["powershell.exe"])];
        first_build_completed_at = Some(Instant::now());
        entries
    });
    assert!(cache.cached.as_ref().unwrap().built_at >= first_build_completed_at.unwrap());
    let second = cache.snapshot(Duration::from_secs(60), || {
        builds += 1;
        Vec::new()
    });
    let refreshed = cache.snapshot(Duration::ZERO, || {
        builds += 1;
        vec![test_entry(20, 1, "pwsh.exe", &["pwsh.exe"])]
    });

    assert!(Arc::ptr_eq(&first, &second));
    assert!(!Arc::ptr_eq(&second, &refreshed));
    assert_eq!(builds, 2);
    assert_eq!(refreshed[0].pid, 20);
}

#[test]
fn windows_process_tree_selects_wrapped_agent_descendant() {
    let entries = vec![
        test_entry(10, 1, "cmd.exe", &["cmd.exe"]),
        test_entry(
            20,
            10,
            "node.exe",
            &[
                "node.exe",
                "C:\\Users\\gterm\\AppData\\Roaming\\npm\\node_modules\\codex\\bin\\codex.js",
            ],
        ),
    ];

    let job = super::select_pane_foreground_job(10, &entries).unwrap();

    assert_eq!(job.process_group_id, 20);
    assert_eq!(job.processes[0].name, "node.exe");
}

#[test]
fn windows_process_tree_selects_cmd_wrapped_agent_descendant() {
    let entries = vec![
        test_entry(10, 1, "powershell.exe", &["powershell.exe"]),
        test_entry(
            20,
            10,
            "cmd.exe",
            &[
                "cmd.exe",
                "/D",
                "/S",
                "/C",
                "C:\\Users\\gterm\\AppData\\Roaming\\npm\\codex.cmd --model gpt-5",
            ],
        ),
    ];

    let job = super::select_pane_foreground_job(10, &entries).unwrap();

    assert_eq!(job.process_group_id, 20);
    assert_eq!(job.processes[0].name, "cmd.exe");
}

#[test]
fn windows_process_tree_selects_topmost_codex_process_in_single_agent_chain() {
    let entries = vec![
            test_entry(10, 1, "powershell.exe", &["powershell.exe"]),
            test_entry(
                20,
                10,
                "node.exe",
                &[
                    "node.exe",
                    "C:\\Users\\gterm\\AppData\\Roaming\\npm\\node_modules\\@openai\\codex\\bin\\codex.js",
                ],
            ),
            test_entry(
                30,
                20,
                "codex.exe",
                &["C:\\Users\\gterm\\AppData\\Roaming\\npm\\node_modules\\@openai\\codex\\node_modules\\@openai\\codex-win32-x64\\vendor\\x86_64-pc-windows-msvc\\bin\\codex.exe"],
            ),
            test_entry(40, 30, "node_repl.exe", &["node_repl.exe"]),
            test_entry(
                50,
                40,
                "codex.exe",
                &["codex.exe", "app-server", "--listen", "stdio://"],
            ),
        ];

    let job = super::select_pane_foreground_job(10, &entries).unwrap();

    assert_eq!(job.process_group_id, 20);
    assert_eq!(job.processes[0].name, "node.exe");
}

#[test]
fn windows_process_tree_keeps_topmost_agent_over_different_agent_descendant() {
    let entries = vec![
        test_entry(10, 1, "powershell.exe", &["powershell.exe"]),
        test_entry(20, 10, "claude.exe", &["claude.exe"]),
        test_entry(
            30,
            20,
            "cmd.exe",
            &["cmd.exe", "/D", "/S", "/C", "codex mcp-server"],
        ),
    ];

    let job = super::select_pane_foreground_job(10, &entries).unwrap();

    assert_eq!(job.process_group_id, 20);
    assert_eq!(job.processes[0].name, "claude.exe");
}

#[test]
fn windows_process_tree_keeps_root_agent_over_agent_descendant() {
    let entries = vec![
        test_entry(10, 1, "claude.exe", &["claude.exe"]),
        test_entry(
            20,
            10,
            "cmd.exe",
            &["cmd.exe", "/D", "/S", "/C", "codex mcp-server"],
        ),
    ];

    let job = super::select_pane_foreground_job(10, &entries).unwrap();

    assert_eq!(job.process_group_id, 10);
    assert_eq!(job.processes[0].name, "claude.exe");
}

#[test]
fn windows_process_tree_returns_shell_for_same_agent_siblings() {
    let entries = vec![
        test_entry(10, 1, "powershell.exe", &["powershell.exe"]),
        test_entry(20, 10, "codex.exe", &["codex.exe"]),
        test_entry(30, 10, "codex.exe", &["codex.exe"]),
    ];

    let job = super::select_pane_foreground_job(10, &entries).unwrap();

    assert_eq!(job.process_group_id, 10);
    assert_eq!(job.processes[0].name, "powershell.exe");
}

#[test]
fn windows_process_tree_returns_shell_for_plain_descendant() {
    let entries = vec![
        test_entry(10, 1, "powershell.exe", &["powershell.exe"]),
        test_entry(20, 10, "git.exe", &["git.exe", "status"]),
    ];

    let job = super::select_pane_foreground_job(10, &entries).unwrap();

    assert_eq!(job.process_group_id, 10);
    assert_eq!(job.processes[0].name, "powershell.exe");
}

#[test]
fn windows_shell_is_available_only_without_descendants() {
    let shell_only = vec![test_entry(10, 1, "powershell.exe", &["powershell.exe"])];
    assert_eq!(
        super::available_pane_shell_from_snapshot(10, &shell_only).as_deref(),
        Some("powershell.exe")
    );

    let busy = vec![
        test_entry(10, 1, "powershell.exe", &["powershell.exe"]),
        test_entry(20, 10, "git.exe", &["git.exe", "status"]),
    ];
    assert_eq!(super::available_pane_shell_from_snapshot(10, &busy), None);

    let replaced = vec![test_entry(10, 1, "vim.exe", &["vim.exe"])];
    assert_eq!(
        super::available_pane_shell_from_snapshot(10, &replaced),
        None
    );
}

#[test]
fn windows_process_tree_returns_shell_for_multiple_agent_descendants() {
    let entries = vec![
        test_entry(10, 1, "powershell.exe", &["powershell.exe"]),
        test_entry(20, 10, "codex.exe", &["codex.exe"]),
        test_entry(30, 10, "claude.exe", &["claude.exe"]),
    ];

    let job = super::select_pane_foreground_job(10, &entries).unwrap();

    assert_eq!(job.process_group_id, 10);
    assert_eq!(job.processes[0].name, "powershell.exe");
}

#[test]
fn windows_session_processes_collects_shell_and_descendants() {
    let entries = vec![
        test_entry(10, 1, "powershell.exe", &["powershell.exe"]),
        test_entry(20, 10, "cmd.exe", &["cmd.exe"]),
        test_entry(30, 20, "node.exe", &["node.exe"]),
        test_entry(40, 1, "unrelated.exe", &["unrelated.exe"]),
    ];

    let mut pids = super::session_processes_from_entries(10, &entries);
    pids.sort_unstable();

    assert_eq!(pids, vec![10, 20, 30]);
}

#[test]
fn windows_process_tree_ignores_pid_reuse_cycles() {
    let entries = vec![
        test_entry(10, 30, "powershell.exe", &["powershell.exe"]),
        test_entry(20, 10, "codex.exe", &["codex.exe"]),
        test_entry(30, 20, "node.exe", &["node.exe"]),
    ];

    let descendants = super::descendant_entries(10, &entries);

    assert_eq!(
        descendants
            .iter()
            .map(|entry| entry.pid)
            .collect::<Vec<_>>(),
        vec![20, 30]
    );
}

#[test]
fn windows_process_tree_returns_shell_when_candidate_parent_chain_cycles() {
    let entries = vec![
        test_entry(10, 40, "powershell.exe", &["powershell.exe"]),
        test_entry(20, 10, "codex.exe", &["codex.exe"]),
        test_entry(30, 10, "codex.exe", &["codex.exe"]),
        test_entry(40, 10, "node.exe", &["node.exe"]),
    ];

    let job = super::select_pane_foreground_job(10, &entries).unwrap();

    assert_eq!(job.process_group_id, 10);
    assert_eq!(job.processes[0].name, "powershell.exe");
}

#[test]
fn scrollback_editor_argv_uses_editor_env_and_appends_path() {
    let path = std::path::Path::new(r"C:\Users\User\AppData\Local\Temp\gterm scrollback.txt");
    let argv = super::scrollback_editor_argv_with_env(
        path,
        Some(r#""C:\Program Files\Microsoft VS Code\Code.exe" --wait"#),
    )
    .unwrap();

    assert_eq!(argv[0], r"C:\Program Files\Microsoft VS Code\Code.exe");
    assert_eq!(argv[1], "--wait");
    assert_eq!(argv[2], path.display().to_string());
}

#[test]
fn scrollback_editor_argv_falls_back_to_notepad() {
    let path = std::path::Path::new(r"C:\Temp\gterm-scrollback.txt");
    let argv = super::scrollback_editor_argv_with_env(path, None).unwrap();

    assert_eq!(
        argv,
        vec!["notepad.exe".to_string(), path.display().to_string()]
    );
}

fn test_entry(pid: u32, parent_pid: u32, name: &str, argv: &[&str]) -> super::WindowsProcessEntry {
    super::WindowsProcessEntry {
        pid,
        parent_pid,
        name: name.to_string(),
        argv0: argv.first().map(|value| (*value).to_string()),
        argv: Some(argv.iter().map(|value| (*value).to_string()).collect()),
        cmdline: Some(argv.join(" ")),
    }
}

#[test]
fn process_environment_variable_parser_reads_case_insensitive_marker() {
    let environment: Vec<u16> = "PATH=C:\\Windows\0gterm_pane_runtime_id=pane-a\0\0"
        .encode_utf16()
        .collect();

    assert_eq!(
        super::environment_variable_from_utf16(&environment, super::PANE_RUNTIME_MARKER_ENV_VAR,)
            .as_deref(),
        Some("pane-a")
    );
}

#[test]
fn pane_runtime_markers_are_distinct() {
    let first = super::next_pane_runtime_marker();
    let second = super::next_pane_runtime_marker();

    assert_ne!(first, second);
}

#[test]
fn pane_runtime_marker_is_added_only_to_git_bash_environment() {
    let root = std::env::temp_dir().join(format!(
        "gterm-git-bash-test-{}",
        super::next_pane_runtime_marker()
    ));
    fs::create_dir_all(root.join("bin")).expect("create Git Bash bin fixture");
    fs::create_dir_all(root.join("usr").join("bin")).expect("create Git Bash usr/bin fixture");
    fs::create_dir_all(root.join("cmd")).expect("create Git Bash cmd fixture");
    fs::write(root.join("bin").join("bash.exe"), []).expect("create Bash fixture");
    fs::write(root.join("usr").join("bin").join("msys-2.0.dll"), [])
        .expect("create MSYS runtime fixture");
    fs::write(root.join("cmd").join("git.exe"), []).expect("create Git fixture");

    let mut git_bash = portable_pty::CommandBuilder::new(root.join("bin").join("bash.exe"));
    super::apply_pane_runtime_marker_platform(&mut git_bash);
    let mut path_resolved_git_bash = portable_pty::CommandBuilder::new("bash.exe");
    path_resolved_git_bash.env("PATH", root.join("bin"));
    super::apply_pane_runtime_marker_platform(&mut path_resolved_git_bash);
    let mut cmd = portable_pty::CommandBuilder::new("cmd.exe");
    super::apply_pane_runtime_marker_platform(&mut cmd);

    assert!(git_bash
        .get_env(super::PANE_RUNTIME_MARKER_ENV_VAR)
        .is_some_and(|value| !value.is_empty()));
    assert!(path_resolved_git_bash
        .get_env(super::PANE_RUNTIME_MARKER_ENV_VAR)
        .is_some_and(|value| !value.is_empty()));
    assert!(cmd.get_env(super::PANE_RUNTIME_MARKER_ENV_VAR).is_none());
    fs::remove_dir_all(root).expect("remove Git Bash fixture");
}

#[test]
fn ime_open_reflects_open_status() {
    // IMC_GETOPENSTATUS returns nonzero when the IME is open (Hangul
    // composing) and zero for direct English/ASCII input.
    assert!(super::ime_open(1));
    assert!(!super::ime_open(0));
    // Any nonzero value is treated as open, not just 1.
    assert!(super::ime_open(2));
}

#[test]
fn toggle_key_maps_korean_and_ignores_other_languages() {
    // Korean (0x0412) -> Hangul/English toggle.
    assert_eq!(
        super::toggle_key_for_language(0x0412),
        Some(super::VK_HANGUL)
    );
    // Korean with a different sublanguage still resolves by primary id.
    assert_eq!(
        super::toggle_key_for_language(0x0812),
        Some(super::VK_HANGUL)
    );
    // Japanese (0x0411) and Chinese (0x0804) have no mapped key yet.
    assert_eq!(super::toggle_key_for_language(0x0411), None);
    assert_eq!(super::toggle_key_for_language(0x0804), None);
    // English (0x0409): nothing to toggle.
    assert_eq!(super::toggle_key_for_language(0x0409), None);
}

#[test]
fn send_vk_tap_reports_success_when_full_tap_is_queued() {
    let mut calls = 0;
    let ok = super::send_vk_tap_with(super::VK_HANGUL, |events| {
        calls += 1;
        events.len() as u32
    });
    assert!(ok, "a fully queued tap is reported as success");
    assert_eq!(calls, 1, "a clean tap needs no retry");
}

#[test]
fn send_vk_tap_retries_keyup_and_reports_toggle_on_partial_injection() {
    let mut calls = 0;
    let mut retry_len = 0;
    let mut retry_is_keyup = false;
    let ok = super::send_vk_tap_with(super::VK_HANGUL, |events| {
        calls += 1;
        if calls == 1 {
            // Only the key-down is queued; the key-up is dropped.
            1
        } else {
            retry_len = events.len();
            // SAFETY: keyboard inputs, so reading the `ki` union is valid.
            retry_is_keyup = unsafe { events[0].Anonymous.ki.dwFlags } == super::KEYEVENTF_KEYUP;
            events.len() as u32
        }
    });
    assert!(ok, "the queued key-down may have toggled the IME");
    assert_eq!(calls, 2, "the dropped key-up is retried exactly once");
    assert_eq!(retry_len, 1, "only the key-up is retried");
    assert!(retry_is_keyup, "the retry injects the key-up event");
}

#[test]
fn send_vk_tap_reports_toggle_when_keyup_retry_fails() {
    let mut calls = 0;
    let ok = super::send_vk_tap_with(super::VK_HANGUL, |_events| {
        calls += 1;
        if calls == 1 {
            1
        } else {
            0
        }
    });
    assert!(ok, "the queued key-down may have toggled the IME");
    assert_eq!(calls, 2, "the dropped key-up is retried exactly once");
}

#[test]
fn send_vk_tap_reports_failure_without_retry_when_nothing_is_queued() {
    let mut calls = 0;
    let ok = super::send_vk_tap_with(super::VK_HANGUL, |_events| {
        calls += 1;
        0
    });
    assert!(!ok, "a fully blocked tap is reported as failure");
    assert_eq!(
        calls, 1,
        "nothing was queued, so there is no key-up to retry"
    );
}

#[test]
fn key_tap_inputs_emit_keydown_then_keyup() {
    let inputs = super::key_tap_inputs(super::VK_HANGUL);
    // SAFETY: both entries are keyboard inputs, so reading the `ki` union is valid.
    unsafe {
        assert_eq!(inputs[0].r#type, super::INPUT_KEYBOARD);
        assert_eq!(inputs[0].Anonymous.ki.wVk, super::VK_HANGUL);
        assert_eq!(inputs[0].Anonymous.ki.dwFlags, 0, "first event is key-down");
        assert_eq!(inputs[1].r#type, super::INPUT_KEYBOARD);
        assert_eq!(inputs[1].Anonymous.ki.wVk, super::VK_HANGUL);
        assert_eq!(
            inputs[1].Anonymous.ki.dwFlags,
            super::KEYEVENTF_KEYUP,
            "second event is key-up"
        );
    }
}
