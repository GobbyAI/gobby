use std::process::Command;

#[test]
fn text_output_uses_renderer() {
    let output = Command::new(env!("CARGO_BIN_EXE_gwiki"))
        .args(["--format", "text", "search", "--topic", "rust", "ownership"])
        .output()
        .expect("gwiki binary runs");

    assert!(
        output.status.success(),
        "search failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("Search results for \"ownership\""));
    assert!(stdout.contains("Scope: topic:rust"));
    assert!(!stdout.contains("CommandResult"));
    assert!(!stdout.contains("SearchOutput"));
}

#[test]
fn status_goes_to_stderr() {
    let output = Command::new(env!("CARGO_BIN_EXE_gwiki"))
        .args(["--format", "json", "status", "--topic", "rust"])
        .output()
        .expect("gwiki binary runs");

    assert!(
        output.status.success(),
        "status failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );

    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stdout.trim_start().starts_with('{'), "{stdout}");
    assert!(stdout.contains("\"command\": \"status\""), "{stdout}");
    assert!(!stdout.contains("gwiki:"), "{stdout}");
    assert!(
        stderr.contains("gwiki: status resolved scope topic:rust"),
        "{stderr}"
    );
}
