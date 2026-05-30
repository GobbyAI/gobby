use std::fs;
use std::process::Command;

fn assert_vault_shape(root: &std::path::Path) {
    for dir in [
        "raw",
        "raw/assets",
        "wiki/sources",
        "wiki/concepts",
        "wiki/topics",
        "inbox",
        "outputs",
        "meta/health",
        ".gwiki",
    ] {
        assert!(root.join(dir).is_dir(), "missing directory {dir}");
    }

    for file in ["raw/INDEX.md", "_index.md", "log.md"] {
        assert!(root.join(file).is_file(), "missing file {file}");
    }
}

#[test]
fn init_creates_vault_shape() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let hub = tmp.path().join("hub");
    let topic_status = Command::new(env!("CARGO_BIN_EXE_gwiki"))
        .args(["init", "--topic", "rust"])
        .env("GOBBY_WIKI_HUB", &hub)
        .current_dir(tmp.path())
        .status()
        .expect("run topic init");

    assert!(topic_status.success());
    assert_vault_shape(&hub.join("topics").join("rust"));
    assert!(hub.join("wikis.json").is_file());

    let project = tmp.path().join("project");
    let gobby_dir = project.join(".gobby");
    fs::create_dir_all(&gobby_dir).expect("create .gobby");
    let project_json = gobby_dir.join("project.json");
    let original_project_json = r#"{
  "id": "project-123",
  "name": "demo"
}
"#;
    fs::write(&project_json, original_project_json).expect("write project json");

    let project_status = Command::new(env!("CARGO_BIN_EXE_gwiki"))
        .args(["init", "--project"])
        .env_remove("GOBBY_WIKI_HUB")
        .current_dir(&project)
        .status()
        .expect("run project init");

    assert!(project_status.success());
    assert_vault_shape(&project.join(".gobby").join("wiki"));
    assert!(
        project
            .join(".gobby")
            .join("wiki")
            .join("wikis.json")
            .is_file()
    );
    assert_eq!(
        fs::read_to_string(project_json).expect("read project json"),
        original_project_json
    );
}
