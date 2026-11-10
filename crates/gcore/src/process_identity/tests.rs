use super::*;
use std::collections::BTreeMap;

fn proc(name: &str, parent: Option<u32>, argv: Option<&[&str]>) -> ProcessInfo {
    ProcessInfo {
        parent,
        name: name.to_owned(),
        argv: argv.map(|args| args.iter().map(|arg| (*arg).to_owned()).collect()),
    }
}

fn table(entries: Vec<(u32, ProcessInfo)>) -> impl Fn(u32) -> Option<ProcessInfo> {
    let map: BTreeMap<u32, ProcessInfo> = entries.into_iter().collect();
    move |pid| map.get(&pid).cloned()
}

#[test]
fn non_droid_process_is_its_own_cli() {
    let lookup = table(vec![(100, proc("claude", Some(1), Some(&["claude"])))]);

    assert_eq!(stable_cli_pid_with(100, lookup), Some(100));
}

#[test]
fn droid_exec_backend_resolves_to_the_droid_tui_parent() {
    let lookup = table(vec![
        (200, proc("droid", Some(100), Some(&["droid", "exec"]))),
        (100, proc("droid", Some(1), Some(&["droid"]))),
    ]);

    assert_eq!(stable_cli_pid_with(200, lookup), Some(100));
}

#[test]
fn headless_droid_exec_under_droid_stays_its_own_cli() {
    let lookup = table(vec![
        (
            200,
            proc("droid", Some(100), Some(&["droid", "exec", "task"])),
        ),
        (
            100,
            proc("droid", Some(1), Some(&["droid", "exec", "--auto"])),
        ),
    ]);

    assert_eq!(stable_cli_pid_with(200, lookup), Some(200));
}

#[test]
fn droid_under_a_non_droid_parent_stays_its_own_cli() {
    let lookup = table(vec![
        (200, proc("droid", Some(100), Some(&["droid"]))),
        (100, proc("zsh", Some(1), Some(&["-zsh"]))),
    ]);

    assert_eq!(stable_cli_pid_with(200, lookup), Some(200));
}

#[test]
fn unreadable_own_process_fails_closed() {
    let lookup = table(Vec::new());

    assert_eq!(stable_cli_pid_with(200, lookup), None);
}

#[test]
fn unreadable_parent_falls_back_to_the_process() {
    let lookup = table(vec![(200, proc("droid", Some(100), Some(&["droid"])))]);

    assert_eq!(stable_cli_pid_with(200, lookup), Some(200));
}

#[test]
fn parent_without_readable_arguments_falls_back_to_the_process() {
    let lookup = table(vec![
        (200, proc("droid", Some(100), Some(&["droid"]))),
        (100, proc("droid", Some(1), None)),
    ]);

    assert_eq!(stable_cli_pid_with(200, lookup), Some(200));
}

#[test]
fn parent_with_only_a_program_name_is_the_cli() {
    let lookup = table(vec![
        (200, proc("droid", Some(100), Some(&["droid"]))),
        (100, proc("droid", Some(1), Some(&["droid"]))),
    ]);

    assert_eq!(stable_cli_pid_with(200, lookup), Some(100));
}

#[test]
fn droid_without_a_parent_stays_its_own_cli() {
    let lookup = table(vec![(200, proc("droid", None, Some(&["droid"])))]);

    assert_eq!(stable_cli_pid_with(200, lookup), Some(200));
}

#[cfg(any(target_os = "linux", test))]
#[test]
fn stat_takes_the_name_between_the_first_and_last_paren() {
    assert_eq!(
        parse_stat("4242 (my (droid) x) S 77 4242 4242 0 -1 4194560"),
        Some(("my (droid) x".to_owned(), 77))
    );
}

#[cfg(any(target_os = "linux", test))]
#[test]
fn cmdline_splits_nul_terminated_arguments() {
    assert_eq!(parse_cmdline(b"droid\0exec\0"), vec!["droid", "exec"]);
    assert!(parse_cmdline(b"").is_empty());
}

#[cfg(any(target_os = "macos", test))]
#[test]
fn procargs_reads_arguments_after_the_exec_path_and_padding() {
    let mut buffer = 2_i32.to_ne_bytes().to_vec();
    buffer.extend_from_slice(b"/usr/local/bin/droid\0\0\0droid\0exec\0PATH=/bin\0");

    assert_eq!(
        parse_procargs2(&buffer),
        Some(vec!["droid".to_owned(), "exec".to_owned()])
    );
}

#[cfg(any(target_os = "macos", test))]
#[test]
fn procargs_refuses_a_buffer_shorter_than_its_argc() {
    let mut buffer = 3_i32.to_ne_bytes().to_vec();
    buffer.extend_from_slice(b"/usr/local/bin/droid\0\0\0droid\0exec\0");

    assert_eq!(parse_procargs2(&buffer), None);
}

#[cfg(unix)]
#[test]
fn live_process_reports_its_parent_and_arguments() {
    let me = std::process::id();
    // SAFETY: getppid has no preconditions and cannot fail.
    let parent = unsafe { libc::getppid() } as u32;

    let info = process_info(me).expect("own process info is readable");

    assert_eq!(info.parent, Some(parent));
    assert!(info.argv.is_some_and(|argv| !argv.is_empty()));
    assert_eq!(stable_cli_pid(me), Some(me));
    assert_eq!(current_cli_pid(), stable_cli_pid(parent));
}
