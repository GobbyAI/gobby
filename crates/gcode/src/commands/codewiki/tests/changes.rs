use super::support::*;
use super::*;

#[test]
fn codewiki_changes_baseline_persists_snapshot_and_degrades_without_graph() {
    let project = tempfile::tempdir().expect("project tempdir");
    std::fs::create_dir_all(project.path().join("src")).expect("source dir");
    std::fs::write(project.path().join("src/lib.rs"), "pub struct Client;\n").expect("write lib");
    let out_dir = project.path().join("codewiki");
    let input = CodewikiInput {
        files: vec!["src/lib.rs".to_string()],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Unavailable,
        symbols: vec![test_symbol(
            "src/lib.rs",
            "Client",
            "class",
            1,
            "pub struct Client;",
        )],
    };

    let snapshot = build_codewiki_index_snapshot(project.path(), &input).expect("snapshot");
    let changes = build_codewiki_changes_doc(None, &snapshot).expect("changes doc");
    assert!(changes.contains("baseline: true"));
    assert!(changes.contains("degraded: true"));
    assert!(changes.contains("graph-unavailable"));
    assert!(changes.contains("No previous index snapshot was available."));
    assert!(!changes.contains("## Changed Graph Neighborhoods"));

    write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &[("code/_changes.md".to_string(), changes)],
        Some(snapshot),
        "off",
    )
    .expect("write snapshot");
    let meta = read_codewiki_meta(&out_dir).expect("read meta");
    let persisted = meta.index_snapshot.expect("persisted snapshot");
    assert_eq!(persisted.files["src/lib.rs"].symbol_count, 1);
    assert!(persisted.files["src/lib.rs"].content_hash.len() >= 32);
}

#[test]
fn codewiki_changes_lists_index_diff_against_previous_snapshot() {
    let project = tempfile::tempdir().expect("project tempdir");
    std::fs::create_dir_all(project.path().join("src")).expect("source dir");
    std::fs::write(project.path().join("src/lib.rs"), "pub struct Client;\n").expect("write lib");
    std::fs::write(project.path().join("src/api.rs"), "pub fn serve() {}\n").expect("write api");

    let previous_symbols = vec![
        test_symbol("src/lib.rs", "Client", "class", 1, "pub struct Client;"),
        test_symbol("src/api.rs", "serve", "function", 1, "pub fn serve()"),
    ];
    let previous_input = CodewikiInput {
        files: vec!["src/lib.rs".to_string(), "src/api.rs".to_string()],
        graph_edges: vec![CodewikiGraphEdge::call(
            previous_symbols[0].id.clone(),
            previous_symbols[1].id.clone(),
        )],
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: previous_symbols,
    };
    let previous =
        build_codewiki_index_snapshot(project.path(), &previous_input).expect("previous snapshot");

    std::fs::write(
        project.path().join("src/lib.rs"),
        "pub struct Client;\npub fn connect() {}\n",
    )
    .expect("modify lib");
    std::fs::remove_file(project.path().join("src/api.rs")).expect("remove api");
    std::fs::write(project.path().join("src/new.rs"), "pub fn start() {}\n").expect("write new");
    let current_symbols = vec![
        test_symbol("src/lib.rs", "Client", "class", 1, "pub struct Client;"),
        test_symbol("src/lib.rs", "connect", "function", 2, "pub fn connect()"),
        test_symbol("src/new.rs", "start", "function", 1, "pub fn start()"),
    ];
    let current_input = CodewikiInput {
        files: vec!["src/lib.rs".to_string(), "src/new.rs".to_string()],
        graph_edges: vec![CodewikiGraphEdge::call(
            current_symbols[0].id.clone(),
            current_symbols[1].id.clone(),
        )],
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: current_symbols,
    };
    let current =
        build_codewiki_index_snapshot(project.path(), &current_input).expect("current snapshot");

    let changes = build_codewiki_changes_doc(Some(&previous), &current).expect("changes doc");
    assert!(changes.contains("baseline: false"));
    assert!(changes.contains("degraded: false"));
    assert!(changes.contains("- `src/new.rs`"));
    assert!(changes.contains("- `src/api.rs`"));
    assert!(changes.contains("- `src/lib.rs`"));
    assert!(changes.contains("`connect` function in `src/lib.rs`"));
    assert!(changes.contains("`start` function in `src/new.rs`"));
    assert!(changes.contains("`serve` function in `src/api.rs`"));
    assert!(changes.contains("## Changed Graph Neighborhoods"));
    assert!(changes.contains("`Client` class in `src/lib.rs`"));
}
