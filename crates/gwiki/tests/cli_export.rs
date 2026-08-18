use std::fs;

mod common;

#[test]
fn export_workflow_assets_writes_outputs_without_mutating_wiki_pages() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let hub = tmp.path().join("hub");
    let vault = hub.join("rust");
    let wiki_page = vault.join("knowledge/topics/ownership.md");
    fs::create_dir_all(wiki_page.parent().expect("wiki parent")).expect("create wiki dir");
    fs::write(&wiki_page, "# Ownership\n\nCanonical page.\n").expect("write wiki page");
    let before = fs::read_to_string(&wiki_page).expect("read before");

    let gobby_home = tmp.path().join("gobby-home");
    fs::create_dir_all(&gobby_home).expect("gobby home");
    let output = common::gwiki_command()
        .args([
            "--topic",
            "rust",
            "export",
            "workflow-assets",
            "--output",
            "workflow-bundle.md",
        ])
        .env("GOBBY_WIKI_HUB", &hub)
        .env("GOBBY_HOME", &gobby_home)
        .current_dir(tmp.path())
        .output()
        .expect("gwiki binary runs");

    assert!(
        output.status.success(),
        "export failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );

    let export_path = vault.join("outputs/workflow-bundle.md");
    let exported = fs::read_to_string(&export_path).expect("read export");
    assert!(exported.contains("# GWiki Workflow Assets"));
    assert!(exported.contains("## compile"));
    assert_eq!(fs::read_to_string(&wiki_page).expect("read after"), before);

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("\"command\": \"export\""), "{stdout}");
    assert!(stdout.contains("workflow-bundle.md"), "{stdout}");

    let report = tmp.path().join("health.md");
    fs::write(&report, "# Health\n\nGenerated report.\n").expect("write report");
    let output = common::gwiki_command()
        .args([
            "--topic",
            "rust",
            "export",
            "report",
            "--from",
            report.to_str().expect("utf8 path"),
            "--output",
            "reports/health.md",
        ])
        .env("GOBBY_WIKI_HUB", &hub)
        .env("GOBBY_HOME", &gobby_home)
        .current_dir(tmp.path())
        .output()
        .expect("gwiki binary runs");

    assert!(
        output.status.success(),
        "report export failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        fs::read_to_string(vault.join("outputs/reports/health.md")).expect("report export"),
        "# Health\n\nGenerated report.\n"
    );
    assert_eq!(fs::read_to_string(&wiki_page).expect("read final"), before);
}

#[test]
fn export_pages_writes_json_siblings() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let hub = tmp.path().join("hub");
    let vault = hub.join("rust");
    let wiki_page = vault.join("knowledge/topics/ownership.md");
    fs::create_dir_all(wiki_page.parent().expect("wiki parent")).expect("create wiki dir");
    fs::write(
        &wiki_page,
        "---\ntitle: Ownership\n---\n# Ownership\n\nSee [[Borrowing]].\n",
    )
    .expect("write wiki page");

    let gobby_home = tmp.path().join("gobby-home");
    fs::create_dir_all(&gobby_home).expect("gobby home");
    let output = common::gwiki_command()
        .args(["--topic", "rust", "export", "pages"])
        .env("GOBBY_WIKI_HUB", &hub)
        .env("GOBBY_HOME", &gobby_home)
        .current_dir(tmp.path())
        .output()
        .expect("gwiki binary runs");

    assert!(
        output.status.success(),
        "export pages failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );

    let sibling = vault.join("outputs/pages/knowledge/topics/ownership.json");
    let json: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(&sibling).expect("read sibling"))
            .expect("valid sibling json");
    assert_eq!(json["path"], "knowledge/topics/ownership.md");
    assert_eq!(json["frontmatter"]["title"], "Ownership");
    assert_eq!(json["outbound_links"], serde_json::json!(["Borrowing"]));

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("\"command\": \"export\""), "{stdout}");
    assert!(stdout.contains("ownership.json"), "{stdout}");
}
