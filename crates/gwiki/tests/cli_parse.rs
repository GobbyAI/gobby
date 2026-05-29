use std::process::Command;

fn gwiki(args: &[&str]) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_gwiki"))
        .args(args)
        .output()
        .expect("gwiki binary runs")
}

#[test]
fn core_commands_parse_scope_flags() {
    let cases = [
        vec!["init", "--topic", "rust"],
        vec!["setup", "--topic", "rust"],
        vec!["index", "--topic", "rust"],
        vec!["ingest-file", "--topic", "rust", "README.md"],
        vec!["search", "--topic", "rust", "ownership"],
        vec!["backlinks", "--topic", "rust", "wiki/topics/rust.md"],
        vec!["link-suggest", "--topic", "rust"],
        vec!["status", "--topic", "rust"],
        vec!["search", "--project", "ownership"],
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
}
