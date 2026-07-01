mod common;

fn gwiki(args: &[&str]) -> std::process::Output {
    let fixture = common::GwikiFixture::new();
    common::write_gcode_json(fixture.project());
    fixture.output_in_project(args)
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
        let stderr_lower = stderr.to_ascii_lowercase();
        assert!(
            !stderr_lower.contains("error") && !stderr_lower.contains("panic"),
            "{args:?} wrote fatal stderr:\n{stderr}"
        );
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
