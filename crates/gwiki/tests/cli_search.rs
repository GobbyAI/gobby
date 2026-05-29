use std::process::Command;

#[test]
fn search_json_includes_scope() {
    let output = Command::new(env!("CARGO_BIN_EXE_gwiki"))
        .args(["--format", "json", "search", "--topic", "rust", "ownership"])
        .output()
        .expect("gwiki binary runs");

    assert!(
        output.status.success(),
        "search failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );

    let payload: serde_json::Value =
        serde_json::from_slice(&output.stdout).expect("stdout is JSON");
    assert_eq!(payload["command"], "search");
    assert_eq!(payload["query"], "ownership");
    assert_eq!(payload["scope"]["kind"], "topic");
    assert_eq!(payload["scope"]["id"], "rust");
    assert!(payload["results"].is_array());
}
