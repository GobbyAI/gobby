use super::support::*;
use super::*;

fn depth_probe_input() -> CodewikiInput {
    CodewikiInput {
        files: vec!["src/lib.rs".to_string(), "src/nested/api.rs".to_string()],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![
            test_symbol("src/lib.rs", "Client", "class", 1, "pub struct Client;"),
            test_symbol(
                "src/nested/api.rs",
                "serve",
                "function",
                1,
                "pub fn serve()",
            ),
        ],
    }
}

fn generation_systems_at_depth(ai_depth: AiDepth) -> Vec<String> {
    let input = depth_probe_input();
    let mut systems = Vec::new();
    let mut generator = |_prompt: &str, system: &str| {
        systems.push(system.to_string());
        None
    };
    let mut progress = CodewikiProgress::silent();
    let docs = generate_hierarchical_docs_with_progress(
        &input,
        Some(&mut generator),
        ai_depth,
        &mut progress,
    );
    assert!(!docs.is_empty());
    systems
}

#[test]
fn ai_depth_sections_skips_symbol_and_file_generation() {
    let systems = generation_systems_at_depth(AiDepth::Sections);
    assert!(!systems.iter().any(|s| s == prompts::SYMBOL_SYSTEM));
    assert!(!systems.iter().any(|s| s == prompts::FILE_SYSTEM));
    assert!(systems.iter().any(|s| s == prompts::MODULE_SYSTEM));
    assert!(systems.iter().any(|s| s == prompts::REPO_SYSTEM));
    assert!(systems.iter().any(|s| s == prompts::ARCHITECTURE_SYSTEM));
}

#[test]
fn ai_depth_files_skips_symbol_generation_only() {
    let systems = generation_systems_at_depth(AiDepth::Files);
    assert!(!systems.iter().any(|s| s == prompts::SYMBOL_SYSTEM));
    assert!(systems.iter().any(|s| s == prompts::FILE_SYSTEM));
    assert!(systems.iter().any(|s| s == prompts::MODULE_SYSTEM));
}

#[test]
fn ai_depth_symbols_generates_symbol_purposes() {
    let systems = generation_systems_at_depth(AiDepth::Symbols);
    assert!(systems.iter().any(|s| s == prompts::SYMBOL_SYSTEM));
    assert!(systems.iter().any(|s| s == prompts::FILE_SYSTEM));
}

#[test]
fn ai_mode_change_invalidates_unchanged_docs() {
    let project = tempfile::tempdir().expect("project tempdir");
    std::fs::create_dir_all(project.path().join("src")).expect("source dirs");
    std::fs::write(project.path().join("src/lib.rs"), "pub struct Client;\n").expect("write lib");
    let out_dir = project.path().join("codewiki");
    let input = CodewikiInput {
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
    };
    let docs = generate_hierarchical_docs(&input, None);
    let file_doc = "code/files/src/lib.rs.md".to_string();

    write_incremental_doc_set_with_snapshot(project.path(), &out_dir, &docs, None, "off")
        .expect("first write");
    let rewritten =
        write_incremental_doc_set_with_snapshot(project.path(), &out_dir, &docs, None, "sections")
            .expect("mode change write");
    assert!(rewritten.contains(&file_doc));

    let same_mode =
        write_incremental_doc_set_with_snapshot(project.path(), &out_dir, &docs, None, "sections")
            .expect("same mode write");
    assert!(!same_mode.contains(&file_doc));
}
