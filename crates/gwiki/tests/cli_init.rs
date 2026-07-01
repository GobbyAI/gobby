mod common;

fn assert_vault_shape(root: &std::path::Path) {
    for dir in [
        "raw",
        "raw/assets",
        "knowledge/sources",
        "knowledge/concepts",
        "knowledge/topics",
        "inbox",
        "outputs",
        "meta/health",
        "_gwiki",
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
    assert_vault_shape(&project.join("gobby-wiki"));
    assert!(project.join("gobby-wiki").join("wikis.json").is_file());
    common::assert_gcode_json_unchanged(&gcode_json);
}

#[test]
fn init_seeds_obsidian_and_gitignores_inside_git_repo() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let project = tmp.path().join("project");
    std::fs::create_dir_all(project.join(".git")).expect("fake git work tree");
    let gcode_json = common::write_gcode_json(&project);

    let output = common::gwiki_command()
        .args(["init", "--project"])
        .env_remove("GOBBY_WIKI_HUB")
        .current_dir(&project)
        .output()
        .expect("run project init");
    assert!(
        output.status.success(),
        "project init failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );

    let vault = project.join("gobby-wiki");
    let app_json = std::fs::read_to_string(vault.join(".obsidian").join("app.json"))
        .expect("read obsidian app.json");
    assert!(app_json.contains("userIgnoreFilters"));
    assert!(app_json.contains("_gwiki/"));

    let gitignore = std::fs::read_to_string(project.join(".gitignore")).expect("read .gitignore");
    assert_eq!(
        gitignore
            .lines()
            .filter(|l| l.trim() == "gobby-wiki/.obsidian/workspace.json")
            .count(),
        1,
        "exactly one workspace.json rule"
    );
    assert!(
        !gitignore.lines().any(|l| l.trim() == ".obsidian/"),
        "stable Obsidian config should not be ignored wholesale"
    );

    common::assert_gcode_json_unchanged(&gcode_json);
}

#[test]
fn init_outside_git_repo_seeds_obsidian_without_gitignore() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let project = tmp.path().join("project");
    common::write_gcode_json(&project);

    let output = common::gwiki_command()
        .args(["init", "--project"])
        .env_remove("GOBBY_WIKI_HUB")
        .current_dir(&project)
        .output()
        .expect("run project init");
    assert!(output.status.success(), "project init failed");

    let vault = project.join("gobby-wiki");
    assert!(
        vault.join(".obsidian").join("app.json").is_file(),
        "app.json seeded even without git"
    );
    assert!(
        !project.join(".gitignore").exists(),
        "no .gitignore created outside a git work tree"
    );
}
