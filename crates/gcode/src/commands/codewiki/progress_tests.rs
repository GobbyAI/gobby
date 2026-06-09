use super::test_utils::test_symbol;
use super::*;

#[test]
fn codewiki_verbose_progress_captures_generation_order() {
    let project = tempfile::tempdir().expect("project tempdir");
    std::fs::create_dir_all(project.path().join("src")).expect("source dir");
    std::fs::write(project.path().join("src/api.rs"), "pub fn api() {}\n").expect("write api");
    std::fs::write(
        project.path().join("src/lib.rs"),
        "pub struct Client;\npub fn connect() {}\npub fn render() {}\n",
    )
    .expect("write lib");

    let input = CodewikiInput {
        files: vec!["src/api.rs".to_string(), "src/lib.rs".to_string()],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![
            test_symbol("src/lib.rs", "Client", "class", 1, "pub struct Client;"),
            test_symbol("src/lib.rs", "connect", "function", 2, "pub fn connect()"),
            test_symbol("src/lib.rs", "render", "function", 3, "pub fn render()"),
        ],
    };
    let mut progress = CodewikiProgress::capture();
    let mut generator = |prompt: &str, _system: &str| {
        if prompt.contains("src/api.rs") {
            Some("Documents API entry points [src/api.rs:1].".to_string())
        } else {
            Some("Documents library behavior [src/lib.rs:1].".to_string())
        }
    };

    let docs =
        generate_hierarchical_docs_with_progress(&input, Some(&mut generator), &mut progress);
    let changed = write_codewiki_docs(
        project.path(),
        &project.path().join("codewiki"),
        &docs,
        None,
        &OwnershipMeta::default(),
        &mut progress,
    )
    .expect("write docs");
    assert!(!changed.is_empty());

    let lines = progress.into_lines();
    assert!(lines.iter().all(|line| line.starts_with("codewiki: ")));
    assert_before(&lines, "generating file docs for 2 files", "file 1/2 src/api.rs");
    assert_before(&lines, "file 1/2 src/api.rs", "file 2/2 src/lib.rs");
    assert_before(&lines, "file 2/2 src/lib.rs", "symbol 1/3 Client");
    assert_before(&lines, "symbol 1/3 Client", "symbol 3/3 render");
    assert_before(&lines, "generating module docs", "module 1/1 src");
    assert_before(&lines, "module 1/1 src", "generating repo overview");
    assert_before(&lines, "generating repo overview", "generating architecture docs");
    assert_before(&lines, "generating architecture docs", "subsystem 1/1 src");
    assert_before(&lines, "subsystem 1/1 src", "generating onboarding docs");
    assert_before(&lines, "generating onboarding docs", "generating hotspots docs");
    assert_before(&lines, "generating hotspots docs", "writing docs");
}

#[test]
fn codewiki_verbose_progress_silent_sink_emits_no_lines() {
    let mut progress = CodewikiProgress::silent();
    progress.emit("loading indexed files");

    assert!(progress.into_lines().is_empty());

    let docs = generate_hierarchical_docs(&progress_input(), None);
    assert!(docs
        .iter()
        .all(|(_, content)| !content.contains("codewiki:")));
}

#[test]
fn codewiki_verbose_progress_not_serialized_in_summary_json() {
    let summary = CodewikiRunSummary {
        command: "codewiki",
        project_id: "project-1".to_string(),
        project_root: "/repo".to_string(),
        out_dir: "/repo/codewiki".to_string(),
        generated_pages: 3,
        changed_paths: vec!["code/repo.md".to_string()],
        skipped: 2,
        files: 1,
        modules: 1,
        symbols: 3,
        ai_enabled: true,
    };

    let value = serde_json::to_value(summary).expect("summary json");

    assert!(value.get("progress").is_none());
    assert!(value.get("stderr").is_none());
    assert_eq!(value["changed_paths"][0], "code/repo.md");
    assert_eq!(value["ai_enabled"], true);
}

fn progress_input() -> CodewikiInput {
    CodewikiInput {
        files: vec!["src/lib.rs".to_string()],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![test_symbol(
            "src/lib.rs",
            "Client",
            "class",
            1,
            "pub struct Client;",
        )],
    }
}

fn assert_before(lines: &[String], first: &str, second: &str) {
    let first_index = line_index(lines, first);
    let second_index = line_index(lines, second);
    assert!(
        first_index < second_index,
        "expected {first:?} before {second:?}, got {lines:#?}"
    );
}

fn line_index(lines: &[String], needle: &str) -> usize {
    lines
        .iter()
        .position(|line| line.contains(needle))
        .unwrap_or_else(|| panic!("missing progress line containing {needle:?}: {lines:#?}"))
}
