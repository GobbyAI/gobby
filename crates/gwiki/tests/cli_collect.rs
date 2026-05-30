use std::process::{Command, Output};

mod common;

fn gwiki(args: &[&str]) -> Output {
    let tmp = tempfile::tempdir().expect("tempdir");
    let hub = tmp.path().join("hub");
    let project = tmp.path().join("project");
    common::write_project_json(&project);

    Command::new(env!("CARGO_BIN_EXE_gwiki"))
        .args(args)
        .env("GOBBY_WIKI_HUB", &hub)
        .current_dir(&project)
        .output()
        .expect("gwiki binary runs")
}

#[test]
fn collect_parses_scope_flags() {
    let cases = [
        vec!["--format", "text", "--quiet", "collect", "--topic", "rust"],
        vec!["--format", "text", "--quiet", "collect", "--project"],
    ];

    for args in cases {
        let output = gwiki(&args);
        let stdout = String::from_utf8_lossy(&output.stdout);
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(
            output.status.success(),
            "{args:?} failed\nstdout:\n{}\nstderr:\n{}",
            stdout,
            stderr
        );
        assert!(stderr.is_empty(), "{args:?} wrote stderr:\n{stderr}");
        assert!(stdout.contains("Collect ready"), "{stdout}");
        assert!(stdout.contains("Accepted: 0"), "{stdout}");
        assert!(stdout.contains("Skipped: 0"), "{stdout}");
        if args.contains(&"--topic") {
            assert!(stdout.contains("Scope: topic:rust"), "{stdout}");
        } else {
            assert!(
                stdout.contains(&format!("Scope: project:{}", common::PROJECT_ID)),
                "{stdout}"
            );
        }
    }
}
