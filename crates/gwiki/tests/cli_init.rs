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
    let topic_output = common::gwiki_command()
        .args(["init", "--topic", "rust"])
        .env("GOBBY_WIKI_HUB", &hub)
        .current_dir(tmp.path())
        .output()
        .expect("run topic init");

    assert!(
        topic_output.status.success(),
        "topic init failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&topic_output.stdout),
        String::from_utf8_lossy(&topic_output.stderr)
    );
    assert_vault_shape(&hub.join("topics").join("rust"));
    assert!(hub.join("wikis.json").is_file());

    let project = tmp.path().join("project");
    let gcode_json = common::write_gcode_json(&project);

    let project_output = common::gwiki_command()
        .args(["init", "--project"])
        .env_remove("GOBBY_WIKI_HUB")
        .current_dir(&project)
        .output()
        .expect("run project init");

    assert!(
        project_output.status.success(),
        "project init failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&project_output.stdout),
        String::from_utf8_lossy(&project_output.stderr)
    );
    assert_vault_shape(&project.join(".gobby").join("wiki"));
    assert!(
        project
            .join(".gobby")
            .join("wiki")
            .join("wikis.json")
            .is_file()
    );
    common::assert_gcode_json_unchanged(&gcode_json);
}
