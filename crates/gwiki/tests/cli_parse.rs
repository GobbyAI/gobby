use std::fs;

mod common;

fn gwiki(args: &[&str]) -> std::process::Output {
    let fixture = common::GwikiFixture::new();
    common::write_gcode_json(fixture.project());
    fs::write(fixture.project().join("README.md"), "# Parse fixture\n")
        .expect("write ingest fixture");
    if args.first().is_none_or(|command| *command != "init") {
        if args.contains(&"--topic") {
            let mut command = fixture.command_in_project();
            command.args(["init", "--topic", "rust"]);
            let output = command.output().expect("gwiki topic init runs");
            common::assert_success(&output, "topic init fixture");
        } else if args.contains(&"--project") {
            let mut command = fixture.command_in_project();
            command.args(["init", "--project"]);
            let output = command.output().expect("gwiki project init runs");
            common::assert_success(&output, "project init fixture");
        }
    }

    let mut command = fixture.command_in_project();
    command.args(args);
    command.output().expect("gwiki binary runs")
}

#[test]
fn page_write_and_delete_round_trip_via_stdin() {
    let fixture = common::GwikiFixture::new();
    common::write_gcode_json(fixture.project());
    let mut init = fixture.command_in_project();
    init.args(["init", "--topic", "rust"]);
    common::assert_success(&init.output().expect("gwiki topic init runs"), "topic init");

    let mut write = fixture.command_in_project();
    write
        .args([
            "page",
            "write",
            "--topic",
            "rust",
            "--path",
            "knowledge/topics/parse-fixture.md",
        ])
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());
    let mut child = write.spawn().expect("spawn page write");
    {
        use std::io::Write as _;
        child
            .stdin
            .take()
            .expect("piped stdin")
            .write_all(b"# Parse fixture page\n")
            .expect("write page content to stdin");
    }
    let output = child.wait_with_output().expect("page write runs");
    common::assert_success(&output, "page write");
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("\"page-write\""), "stdout:\n{stdout}");
    assert!(stdout.contains("\"created\": true"), "stdout:\n{stdout}");

    let mut delete = fixture.command_in_project();
    delete.args([
        "page",
        "delete",
        "--topic",
        "rust",
        "--path",
        "knowledge/topics/parse-fixture.md",
    ]);
    let output = delete.output().expect("page delete runs");
    common::assert_success(&output, "page delete");
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("\"page-delete\""), "stdout:\n{stdout}");
}

#[test]
fn core_commands_parse_scope_flags() {
    let succeed = [
        vec!["init", "--topic", "rust"],
        vec![
            "read",
            "--topic",
            "rust",
            "--path",
            "knowledge/topics/rust.md",
        ],
        vec!["read", "--topic", "rust", "--title", "Rust"],
        vec!["audit", "--topic", "rust"],
        vec!["lint", "--topic", "rust"],
        vec!["health", "--topic", "rust"],
        vec!["status", "--topic", "rust"],
    ];
    for args in succeed {
        let output = gwiki(&args);
        assert!(
            output.status.success(),
            "{args:?} failed\nstdout:\n{}\nstderr:\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
    }

    let store_backed = [
        vec!["index", "--topic", "rust"],
        vec!["ingest-file", "--topic", "rust", "README.md"],
        vec!["search", "--topic", "rust", "ownership"],
        vec!["backlinks", "--topic", "rust", "knowledge/topics/rust.md"],
        vec!["link-suggest", "--topic", "rust"],
        vec!["--project", "search", "ownership"],
    ];
    for args in store_backed {
        let output = gwiki(&args);
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(
            !output.status.success(),
            "{args:?} must require a grant, not degrade to memory\nstdout:\n{}\nstderr:\n{stderr}",
            String::from_utf8_lossy(&output.stdout),
        );
        assert!(
            stderr.contains("daemon_required")
                || stderr.contains("daemon required")
                || stderr.contains("\"code\": \"malformed\""),
            "{args:?} must fail typed as a grant/daemon error\nstderr:\n{stderr}"
        );
    }

    let quiet = gwiki(&["--quiet", "status", "--topic", "rust"]);
    assert!(quiet.status.success());
    assert_eq!(String::from_utf8_lossy(&quiet.stderr), "");
    let quiet_stdout = String::from_utf8_lossy(&quiet.stdout);
    assert!(
        quiet_stdout.contains("\"grant\"") && !quiet_stdout.contains("shell-ready"),
        "status should report grant state, not shell-ready\n{quiet_stdout}"
    );

    let short_quiet = gwiki(&["status", "--topic", "rust", "-q"]);
    assert!(short_quiet.status.success());
    assert_eq!(String::from_utf8_lossy(&short_quiet.stderr), "");

    let verbose = gwiki(&["-v", "status", "--topic", "rust"]);
    assert!(verbose.status.success());
    assert!(
        String::from_utf8_lossy(&verbose.stderr).contains("verbose diagnostics enabled"),
        "stderr:\n{}",
        String::from_utf8_lossy(&verbose.stderr)
    );

    let conflict = gwiki(&["--quiet", "--verbose", "status", "--topic", "rust"]);
    assert!(!conflict.status.success());
    assert!(
        String::from_utf8_lossy(&conflict.stderr).contains("cannot be used with"),
        "stderr:\n{}",
        String::from_utf8_lossy(&conflict.stderr)
    );
}
