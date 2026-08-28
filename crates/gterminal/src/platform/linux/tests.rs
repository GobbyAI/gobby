use super::*;
use std::sync::{Mutex, OnceLock};
use std::{cell::RefCell, collections::HashMap};

fn env_lock() -> &'static Mutex<()> {
    static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
    LOCK.get_or_init(|| Mutex::new(()))
}

#[test]
fn wsl_marker_detection_matches_kernel_release_text() {
    assert!(text_indicates_wsl("5.15.167.4-microsoft-standard-WSL2"));
    assert!(text_indicates_wsl("4.4.0-19041-Microsoft"));
    assert!(!text_indicates_wsl("6.8.0-64-generic"));
    assert!(!text_indicates_wsl(""));
}

#[test]
fn process_detection_mode_requires_explicit_child_groups_value() {
    assert_eq!(
        parse_process_detection_mode(None),
        Ok(ProcessDetectionMode::Native)
    );
    assert_eq!(
        parse_process_detection_mode(Some("")),
        Ok(ProcessDetectionMode::Native)
    );
    assert_eq!(
        parse_process_detection_mode(Some("native")),
        Ok(ProcessDetectionMode::Native)
    );
    assert_eq!(
        parse_process_detection_mode(Some("child-groups")),
        Ok(ProcessDetectionMode::ChildGroups)
    );
    assert_eq!(parse_process_detection_mode(Some("gvisor")), Err("gvisor"));
}

#[test]
fn child_groups_foreground_group_picks_the_newest_job() {
    let tasks = HashMap::from([(100, vec![100])]);
    let children = HashMap::from([((100, 100), vec![200, 300])]);
    let groups = HashMap::from([(200, 200), (300, 300)]);

    let group = child_groups_foreground_process_group_with(
        100,
        100,
        |pid| tasks.get(&pid).cloned().unwrap_or_default(),
        |pid, tid| children.get(&(pid, tid)).cloned().unwrap_or_default(),
        |pid| groups.get(&pid).copied(),
    );

    assert_eq!(group, Some(300));
}

#[test]
fn child_groups_foreground_group_returns_to_the_shell_group() {
    let tasks = HashMap::from([(100, vec![100])]);
    let children = HashMap::from([((100, 100), vec![150, 160])]);
    let groups = HashMap::from([(150, 90), (160, 90)]);

    let group = child_groups_foreground_process_group_with(
        100,
        90,
        |pid| tasks.get(&pid).cloned().unwrap_or_default(),
        |pid, tid| children.get(&(pid, tid)).cloned().unwrap_or_default(),
        |pid| groups.get(&pid).copied(),
    );

    assert_eq!(group, Some(90));
}

#[test]
fn child_groups_foreground_group_skips_the_shell_group() {
    let tasks = HashMap::from([(100, vec![100])]);
    let children = HashMap::from([((100, 100), vec![150, 160, 300])]);
    let groups = HashMap::from([(150, 90), (160, 90), (300, 300)]);

    let group = child_groups_foreground_process_group_with(
        100,
        90,
        |pid| tasks.get(&pid).cloned().unwrap_or_default(),
        |pid, tid| children.get(&(pid, tid)).cloned().unwrap_or_default(),
        |pid| groups.get(&pid).copied(),
    );

    assert_eq!(group, Some(300));
}

#[test]
fn child_groups_foreground_group_fails_closed_at_the_scan_limit() {
    let children: Vec<u32> = (1..=(CHILD_GROUPS_SCAN_LIMIT as u32 + 10)).collect();
    let mut inspected = 0usize;

    let group = child_groups_foreground_process_group_with(
        100,
        100,
        |_| vec![100],
        |_, _| children.clone(),
        |pid| {
            inspected += 1;
            Some(pid as i32)
        },
    );

    assert_eq!(inspected, CHILD_GROUPS_SCAN_LIMIT);
    assert_eq!(group, None);
}

#[test]
fn foreground_members_follow_the_pane_tree_and_filter_by_process_group() {
    let tasks = HashMap::from([
        (100, vec![100, 101]),
        (200, vec![200]),
        (201, vec![201]),
        (210, vec![210]),
        (220, vec![220]),
        (221, vec![221]),
        (300, vec![300]),
    ]);
    let children = HashMap::from([
        ((100, 100), vec![200, 201, 300]),
        ((100, 101), vec![210]),
        ((200, 200), vec![220]),
        ((220, 220), vec![221]),
    ]);
    let processes = HashMap::from([
        (100, (100, "shell")),
        (200, (200, "leader")),
        (201, (200, "pipeline")),
        (210, (200, "thread-child")),
        (220, (220, "intermediate")),
        (221, (200, "nested-agent")),
        (300, (300, "background")),
        (9999, (200, "unrelated-host-process")),
    ]);
    let task_reads = RefCell::new(Vec::new());
    let child_reads = RefCell::new(Vec::new());
    let member_reads = RefCell::new(Vec::new());

    let members = foreground_process_group_members_with(
        100,
        200,
        |pid| {
            task_reads.borrow_mut().push(pid);
            tasks.get(&pid).cloned().unwrap_or_default()
        },
        |pid, tid| {
            child_reads.borrow_mut().push((pid, tid));
            children.get(&(pid, tid)).cloned().unwrap_or_default()
        },
        |process_group_id, pid| {
            member_reads.borrow_mut().push(pid);
            let (pgrp, comm) = processes.get(&pid)?;
            (*pgrp == process_group_id).then(|| ProcGroupMember {
                pid,
                comm: (*comm).to_string(),
            })
        },
    )
    .unwrap();

    assert_eq!(
        members
            .into_iter()
            .map(|member| (member.pid, member.comm))
            .collect::<Vec<_>>(),
        vec![
            (200, "leader".to_string()),
            (201, "pipeline".to_string()),
            (210, "thread-child".to_string()),
            (221, "nested-agent".to_string()),
        ]
    );
    assert!(child_reads.borrow().contains(&(100, 101)));
    assert!(task_reads.borrow().contains(&220));
    assert!(!task_reads.borrow().contains(&9999));
    assert!(!member_reads.borrow().contains(&9999));
}

#[test]
fn foreground_members_degrade_to_the_direct_group_leader() {
    let members = foreground_process_group_members_with(
        100,
        200,
        |_| Vec::new(),
        |_, _| Vec::new(),
        |process_group_id, pid| {
            (pid == process_group_id).then(|| ProcGroupMember {
                pid,
                comm: "leader".to_string(),
            })
        },
    )
    .unwrap();

    assert_eq!(
        members,
        vec![ProcGroupMember {
            pid: 200,
            comm: "leader".to_string()
        }]
    );
}

#[test]
fn foreground_members_observe_new_children_without_a_snapshot_cache() {
    let children = RefCell::new(HashMap::from([((100, 100), vec![200])]));
    let discover = || {
        foreground_process_group_members_with(
            100,
            200,
            |pid| vec![pid],
            |pid, tid| {
                children
                    .borrow()
                    .get(&(pid, tid))
                    .cloned()
                    .unwrap_or_default()
            },
            |process_group_id, pid| {
                [200, 201]
                    .contains(&pid)
                    .then(|| ProcGroupMember {
                        pid,
                        comm: format!("member-{pid}"),
                    })
                    .filter(|_| process_group_id == 200)
            },
        )
        .unwrap()
        .into_iter()
        .map(|member| member.pid)
        .collect::<Vec<_>>()
    };

    assert_eq!(discover(), vec![200]);
    children.borrow_mut().insert((100, 100), vec![200, 201]);
    assert_eq!(discover(), vec![200, 201]);
}

#[test]
fn proc_stat_parsing_keeps_group_leader_inputs_live() {
    assert_eq!(
        process_pgrp_and_comm_from_stat("123 (name with ) paren) S 1 456 789 0 456"),
        Some((456, "name with ) paren".to_string()))
    );
}

#[test]
fn clipboard_commands_prefer_wayland_when_available() {
    let _guard = env_lock().lock().unwrap();
    unsafe {
        std::env::set_var("WAYLAND_DISPLAY", "wayland-0");
        std::env::remove_var("DISPLAY");
    }
    let commands = clipboard_commands();
    assert_eq!(commands.len(), 1);
    assert_eq!(commands[0].program, "wl-copy");
}

#[test]
fn clipboard_commands_include_x11_fallbacks() {
    let _guard = env_lock().lock().unwrap();
    unsafe {
        std::env::remove_var("WAYLAND_DISPLAY");
        std::env::set_var("DISPLAY", ":0");
    }
    let commands = clipboard_commands();
    assert_eq!(commands.len(), 2);
    assert_eq!(commands[0].program, "xclip");
    assert_eq!(commands[1].program, "xsel");
}

#[test]
fn read_clipboard_text_commands_include_session_backends() {
    let _guard = env_lock().lock().unwrap();
    unsafe {
        std::env::set_var("WAYLAND_DISPLAY", "wayland-0");
        std::env::set_var("DISPLAY", ":0");
    }

    let commands = read_clipboard_text_commands();
    assert_eq!(commands[0].program, "wl-paste");
    assert_eq!(commands[1].program, "wl-paste");
    assert_eq!(commands[2].program, "xclip");
    assert_eq!(commands[3].program, "xsel");
}

#[test]
fn read_clipboard_text_with_command_reads_utf8() {
    let command = ClipboardCommand {
        program: "printf",
        args: &["feature/linear-302"],
    };

    assert_eq!(
        read_clipboard_text_with_command(&command).as_deref(),
        Some("feature/linear-302")
    );
}

#[test]
fn read_clipboard_text_with_command_rejects_oversized_output() {
    let command = ClipboardCommand {
        program: "sh",
        args: &["-c", "yes x | head -c 1048578"],
    };

    assert_eq!(read_clipboard_text_with_command(&command), None);
}

#[test]
fn read_clipboard_image_with_spawned_command_reads_under_limit() {
    let mut command = Command::new("sh");
    command.arg("-c").arg("printf image");

    assert_eq!(
        read_clipboard_image_with_spawned_command_max(command, 16),
        Some(b"image".to_vec())
    );
}

#[test]
fn read_clipboard_image_with_spawned_command_rejects_over_limit() {
    let mut command = Command::new("sh");
    command.arg("-c").arg("printf oversized");

    assert_eq!(
        read_clipboard_image_with_spawned_command_max(command, 4),
        None
    );
}

#[test]
fn read_clipboard_image_rejects_xclip_text_served_for_image_target() {
    let _guard = env_lock().lock().unwrap();
    let temp_dir = std::env::temp_dir().join(format!("gterm-fake-xclip-{}", std::process::id()));
    std::fs::create_dir_all(&temp_dir).expect("temp dir should be created");
    let fake_xclip = temp_dir.join("xclip");
    std::fs::write(&fake_xclip, "#!/bin/sh\nprintf '# Tasks'\n")
        .expect("fake xclip should be written");

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;

        let mut permissions = std::fs::metadata(&fake_xclip)
            .expect("fake xclip metadata")
            .permissions();
        permissions.set_mode(0o700);
        std::fs::set_permissions(&fake_xclip, permissions)
            .expect("fake xclip should be executable");
    }

    let old_path = std::env::var_os("PATH");
    let test_path = match old_path.as_ref() {
        Some(path) => {
            let mut paths = vec![temp_dir.clone()];
            paths.extend(std::env::split_paths(path));
            std::env::join_paths(paths).expect("test path should be valid")
        }
        None => temp_dir.clone().into_os_string(),
    };

    unsafe {
        std::env::remove_var("WAYLAND_DISPLAY");
        std::env::set_var("DISPLAY", ":0");
        std::env::set_var("PATH", test_path);
    }

    let result = read_clipboard_image();

    unsafe {
        match old_path {
            Some(path) => std::env::set_var("PATH", path),
            None => std::env::remove_var("PATH"),
        }
    }
    let _ = std::fs::remove_file(fake_xclip);
    let _ = std::fs::remove_dir(temp_dir);

    assert_eq!(result, None);
}

#[test]
fn read_clipboard_image_rejects_wayland_xclip_fallback_text_for_image_target() {
    let _guard = env_lock().lock().unwrap();
    let temp_dir =
        std::env::temp_dir().join(format!("gterm-fake-wayland-xclip-{}", std::process::id()));
    std::fs::create_dir_all(&temp_dir).expect("temp dir should be created");
    let fake_wl_paste = temp_dir.join("wl-paste");
    let fake_xclip = temp_dir.join("xclip");
    std::fs::write(&fake_wl_paste, "#!/bin/sh\nexit 1\n").expect("fake wl-paste should be written");
    std::fs::write(&fake_xclip, "#!/bin/sh\nprintf '# Tasks'\n")
        .expect("fake xclip should be written");

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;

        for command in [&fake_wl_paste, &fake_xclip] {
            let mut permissions = std::fs::metadata(command)
                .expect("fake clipboard command metadata")
                .permissions();
            permissions.set_mode(0o700);
            std::fs::set_permissions(command, permissions)
                .expect("fake clipboard command should be executable");
        }
    }

    let old_path = std::env::var_os("PATH");
    let test_path = match old_path.as_ref() {
        Some(path) => {
            let mut paths = vec![temp_dir.clone()];
            paths.extend(std::env::split_paths(path));
            std::env::join_paths(paths).expect("test path should be valid")
        }
        None => temp_dir.clone().into_os_string(),
    };

    unsafe {
        std::env::set_var("WAYLAND_DISPLAY", "wayland-0");
        std::env::set_var("DISPLAY", ":0");
        std::env::set_var("PATH", test_path);
    }

    let result = read_clipboard_image();

    unsafe {
        match old_path {
            Some(path) => std::env::set_var("PATH", path),
            None => std::env::remove_var("PATH"),
        }
    }
    let _ = std::fs::remove_file(fake_wl_paste);
    let _ = std::fs::remove_file(fake_xclip);
    let _ = std::fs::remove_dir(temp_dir);

    assert_eq!(result, None);
}

#[test]
fn read_validated_clipboard_image_accepts_real_png_payload() {
    assert_eq!(
        read_validated_clipboard_image(
            "sh",
            &["-c", "printf '\\211PNG\\r\\n\\032\\nrest-of-image'"],
            "png"
        ),
        Some(ClipboardImage {
            bytes: b"\x89PNG\r\n\x1a\nrest-of-image".to_vec(),
            extension: "png",
        })
    );
}

#[test]
fn image_signatures_match_only_their_format() {
    assert!(bytes_match_image_signature("png", b"\x89PNG\r\n\x1a\n..."));
    assert!(bytes_match_image_signature(
        "jpg",
        &[0xFF, 0xD8, 0xFF, 0xE0]
    ));
    assert!(bytes_match_image_signature("gif", b"GIF87a..."));
    assert!(bytes_match_image_signature("gif", b"GIF89a..."));
    assert!(bytes_match_image_signature(
        "webp",
        b"RIFF\x10\x00\x00\x00WEBPVP8 "
    ));

    let mut bmp = vec![0u8; 26];
    bmp[..2].copy_from_slice(b"BM");
    bmp[10] = 26;
    assert!(bytes_match_image_signature("bmp", &bmp));

    assert!(!bytes_match_image_signature("png", b"# Tasks"));
    assert!(!bytes_match_image_signature("jpg", b"plain clipboard text"));
    assert!(!bytes_match_image_signature("gif", b""));
    assert!(!bytes_match_image_signature("webp", b"RIFF but not webp"));
    assert!(!bytes_match_image_signature("bmp", b"\x89PNG\r\n\x1a\n"));
    assert!(!bytes_match_image_signature(
        "bmp",
        b"BM text is not a bitmap"
    ));
    assert!(!bytes_match_image_signature("svg", b"<svg></svg>"));
}

#[test]
fn desktop_notification_separates_option_like_titles() {
    let _guard = env_lock().lock().unwrap();
    unsafe {
        std::env::remove_var("WAYLAND_DISPLAY");
        std::env::set_var("DISPLAY", ":0");
    }

    let path = std::env::temp_dir().join(format!("gterm-notify-send-args-{}", std::process::id()));
    let script = "printf '%s\\n' \"$@\" > \"$GTERM_NOTIFY_ARGS\"";
    let shown = show_desktop_notification_with_command("-danger", Some("body"), |_| {
        let mut cmd = Command::new("sh");
        cmd.arg("-c")
            .arg(script)
            .arg("notify-send")
            .env("GTERM_NOTIFY_ARGS", &path);
        cmd
    })
    .expect("notification command should run");

    assert!(shown);
    let args = std::fs::read_to_string(&path).expect("args file");
    let _ = std::fs::remove_file(&path);
    assert_eq!(args, "--\n-danger\nbody\n");
}

#[test]
fn scrollback_editor_argv_preserves_unix_editor_shell_semantics() {
    let path = std::path::Path::new("/tmp/gterm scrollback.txt");
    let argv = scrollback_editor_argv(path).unwrap();

    assert_eq!(argv[0], "/bin/sh");
    assert_eq!(argv[1], "-c");
    assert!(argv[2].contains("EDITOR:-vi"));
    assert!(argv[2].contains("/tmp/gterm scrollback.txt"));
}
