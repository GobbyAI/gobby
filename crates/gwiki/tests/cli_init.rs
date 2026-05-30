use std::process::Command;

mod common;

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
    let project_json = common::write_project_json(&project);

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
    common::assert_project_json_unchanged(&project_json);
}
