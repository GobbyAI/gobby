use std::process::Command;

fn gwiki(args: &[&str]) -> std::process::Output {
    let tmp = tempfile::tempdir().expect("tempdir");
    let home = tmp.path().join("home");
    std::fs::create_dir_all(&home).expect("create isolated home");
    Command::new(env!("CARGO_BIN_EXE_gwiki"))
        .args(args)
        .env("GOBBY_WIKI_HUB", tmp.path().join("hub"))
        .env("HOME", &home)
        .env_remove("GWIKI_DATABASE_URL")
        .env_remove("GWIKI_POSTGRES_TEST_DATABASE_URL")
        .env_remove("GOBBY_POSTGRES_DSN")
        .env_remove("GCODE_DATABASE_URL")
        .env_remove("GCODE_POSTGRES_TEST_DATABASE_URL")
        .current_dir(tmp.path())
        .output()
        .expect("gwiki binary runs")
}

#[test]
fn text_output_uses_renderer() {
    let output = gwiki(&["--format", "text", "search", "--topic", "rust", "ownership"]);

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
    let output = gwiki(&["--format", "json", "status", "--topic", "rust"]);

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
