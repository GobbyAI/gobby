mod common;

fn gwiki(args: &[&str]) -> std::process::Output {
    common::GwikiFixture::new().output(args)
}

#[test]
fn text_output_uses_renderer() {
    let output = gwiki(&["--format", "text", "search", "--topic", "rust", "ownership"]);

    common::assert_success(&output, "search");

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("Search results for \"ownership\""));
    assert!(stdout.contains("Scope: topic:rust"));
    assert!(!stdout.contains("CommandResult"));
    assert!(!stdout.contains("SearchOutput"));
}

#[test]
fn status_goes_to_stderr() {
    let output = gwiki(&["--format", "json", "status", "--topic", "rust"]);

    common::assert_success(&output, "status");

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
