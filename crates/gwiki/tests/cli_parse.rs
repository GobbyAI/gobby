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
fn core_commands_parse_scope_flags() {
    let cases = [
        vec!["init", "--topic", "rust"],
        vec!["setup", "--topic", "rust"],
        vec!["index", "--topic", "rust"],
        vec!["ingest-file", "--topic", "rust", "README.md"],
        vec!["search", "--topic", "rust", "ownership"],
        vec![
            "read",
            "--topic",
            "rust",
            "--path",
            "knowledge/topics/rust.md",
        ],
        vec!["read", "--topic", "rust", "--title", "Rust"],
        vec!["backlinks", "--topic", "rust", "knowledge/topics/rust.md"],
        vec!["link-suggest", "--topic", "rust"],
        vec!["audit", "--topic", "rust"],
        vec!["lint", "--topic", "rust"],
        vec!["health", "--topic", "rust"],
        vec!["status", "--topic", "rust"],
        vec!["--project", "search", "ownership"],
    ];

    for args in cases {
        let output = gwiki(&args);
        assert!(
            output.status.success(),
            "{args:?} failed\nstdout:\n{}\nstderr:\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
    }

    let quiet = gwiki(&["--quiet", "status", "--topic", "rust"]);
    assert!(quiet.status.success());
    assert_eq!(String::from_utf8_lossy(&quiet.stderr), "");
    assert!(String::from_utf8_lossy(&quiet.stdout).contains("\"status\": \"shell-ready\""));
}
