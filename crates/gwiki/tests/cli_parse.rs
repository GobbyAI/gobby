use std::fs;
use std::process::{Command, Output};

fn gwiki(args: &[&str]) -> Output {
    let tmp = tempfile::tempdir().expect("tempdir");
    let hub = tmp.path().join("hub");
    let project = tmp.path().join("project");
    let gobby_dir = project.join(".gobby");
    fs::create_dir_all(&gobby_dir).expect("create .gobby");
    fs::write(
        gobby_dir.join("project.json"),
        r#"{
  "id": "project-123",
  "name": "parse-fixture"
}
"#,
    )
    .expect("write project json");

    Command::new(env!("CARGO_BIN_EXE_gwiki"))
        .args(args)
        .env("GOBBY_WIKI_HUB", &hub)
        .current_dir(&project)
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
