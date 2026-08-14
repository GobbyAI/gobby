mod common;

fn gwiki(args: &[&str]) -> std::process::Output {
    common::GwikiFixture::new().output(args)
}

#[test]
fn text_output_uses_renderer() {
    let output = gwiki(&["--format", "text", "status", "--topic", "rust"]);

    common::assert_success(&output, "status");

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("Scope: topic:rust"), "{stdout}");
    assert!(stdout.contains("Grant:"), "{stdout}");
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

#[test]
fn ask_llm_rejects_no_ai() {
    let fixture = common::GwikiFixture::new();
    let init = fixture.output(&["init", "--topic", "rust"]);
    common::assert_success(&init, "topic init");

    let output = fixture.output(&[
        "--format",
        "text",
        "ask",
        "--topic",
        "rust",
        "--llm",
        "--no-ai",
        "Which page types does codewiki emit?",
    ]);

    assert!(!output.status.success(), "ask --llm --no-ai must fail");
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("--no-ai") || stderr.contains("cannot be combined"),
        "{stderr}"
    );
}
