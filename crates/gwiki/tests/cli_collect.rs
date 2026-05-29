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
  "name": "collect-fixture"
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
fn collect_parses_scope_flags() {
    let cases = [
        vec!["collect", "--topic", "rust"],
        vec!["collect", "--project"],
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
