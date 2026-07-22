mod common;

fn gwiki(args: &[&str]) -> std::process::Output {
    let fixture = common::GwikiFixture::new();
    common::write_gcode_json(fixture.project());
    fixture.output_in_project(args)
}

#[test]
fn collect_parses_scope_flags() {
    let args = ["--format", "text", "--quiet", "collect", "--topic", "rust"];
    let output = gwiki(&args);
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);

    assert!(output.status.success(), "stderr:\n{stderr}");
    assert!(stdout.contains("Collect ready"), "{stdout}");
    assert!(stdout.contains("Accepted: 0"), "{stdout}");
    assert!(stdout.contains("Skipped: 0"), "{stdout}");
    assert!(stdout.contains("Scope: topic:rust"), "{stdout}");
}

#[test]
fn project_collect_requires_postgres_writer_admission() {
    let output = gwiki(&["--format", "text", "--quiet", "collect", "--project"]);
    let stderr = String::from_utf8_lossy(&output.stderr);

    assert!(!output.status.success());
    assert!(
        stderr.contains("PostgreSQL index is required for gwiki collect"),
        "{stderr}"
    );
}
