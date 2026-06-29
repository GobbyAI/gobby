use super::support::*;
use super::*;

#[test]
fn codewiki_architecture_overview_page_uses_subsystems_and_degradation_metadata() {
    let input = CodewikiInput {
        leading_chunks: std::collections::BTreeMap::new(),
        files: vec![
            "src/api/handler.rs".to_string(),
            "src/api/router.rs".to_string(),
            "src/domain/service.rs".to_string(),
            "src/storage/repo.rs".to_string(),
        ],
        graph_edges: vec![
            CodewikiGraphEdge::call(
                test_component_id("src/api/handler.rs", "handle", "function"),
                test_component_id("src/api/router.rs", "route", "function"),
            ),
            CodewikiGraphEdge::import(
                test_component_id("src/api/handler.rs", "handle", "function"),
                test_component_id("src/domain/service.rs", "Service", "class"),
            ),
            CodewikiGraphEdge::import(
                test_component_id("src/domain/service.rs", "Service", "class"),
                test_component_id("src/storage/repo.rs", "Repo", "class"),
            ),
        ],
        graph_availability: CodewikiGraphAvailability::Truncated,
        symbols: vec![
            test_symbol(
                "src/api/handler.rs",
                "handle",
                "function",
                1,
                "pub fn handle()",
            ),
            test_symbol(
                "src/api/router.rs",
                "route",
                "function",
                1,
                "pub fn route()",
            ),
            test_symbol(
                "src/domain/service.rs",
                "Service",
                "class",
                1,
                "pub struct Service;",
            ),
            test_symbol(
                "src/storage/repo.rs",
                "Repo",
                "class",
                1,
                "pub struct Repo;",
            ),
        ],
    };

    // A generator that attempts and fails marks model degradation; a missing
    // generator (AI off) is structural by intent and records nothing.
    let mut failing_generator = |_prompt: &str, _system: &str, _tier: PromptTier| None;
    let docs = generate_hierarchical_docs(&input, Some(&mut failing_generator));
    let docs_by_path = docs.into_iter().collect::<BTreeMap<_, _>>();
    let rendered = docs_by_path
        .get("code/_architecture.md")
        .expect("architecture overview doc");

    assert!(rendered.contains("generated_by: gcode-codewiki"));
    assert!(rendered.contains("type: code_architecture"));
    // The failing generator is the only content gap: the page degrades on
    // `model-unavailable`, never on graph availability (truncated here).
    assert!(rendered.contains("degraded: true"));
    assert!(rendered.contains("degraded_sources:"));
    assert!(rendered.contains("- model-unavailable"));
    assert!(!rendered.contains("graph-truncated"));
    assert!(rendered.contains("| Subsystem | Responsibility | Child modules |"));
    assert!(rendered.contains("[[code/modules/src/api\\|src/api]]"));
    assert!(rendered.contains("[[code/modules/src/domain\\|src/domain]]"));
    assert!(rendered.contains("[[code/modules/src/storage\\|src/storage]]"));
    assert!(rendered.contains("`src/api` contains 2 direct files and 0 child modules."));
    assert!(rendered.contains("- [src/api/handler.rs:1](src/api/handler.rs#L1)"));
    let body = rendered
        .split_once("</details>\n\n")
        .map(|(_, body)| body)
        .expect("relevant source files details block");
    assert!(!body.contains("src/api/handler.rs:1"));
    assert!(!body.contains("src/api/router.rs:1"));
    for line in rendered
        .lines()
        .filter(|line| line.starts_with("| [[code/modules/"))
    {
        assert_eq!(inline_marker_count(line), 0);
    }
    // The auto-generated subsystem-map mermaid diagram is gone.
    assert!(!rendered.contains("```mermaid"));
    assert!(!rendered.contains("## Subsystem Map"));
}

#[test]
fn architecture_prompt_formats_component_labels_with_raw_ids() {
    let handle_id = test_component_id("src/api/handler.rs", "handle", "function");
    let route_id = test_component_id("src/api/router.rs", "route", "function");
    let input = CodewikiInput {
        leading_chunks: std::collections::BTreeMap::new(),
        files: vec![
            "src/api/handler.rs".to_string(),
            "src/api/router.rs".to_string(),
        ],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![
            test_symbol(
                "src/api/handler.rs",
                "handle",
                "function",
                1,
                "pub fn handle()",
            ),
            test_symbol(
                "src/api/router.rs",
                "route",
                "function",
                1,
                "pub fn route()",
            ),
        ],
    };
    let mut architecture_prompts = Vec::new();
    {
        let mut generator = |prompt: &str, system: &str, _tier: PromptTier| {
            if system == prompts::ARCHITECTURE_SYSTEM {
                architecture_prompts.push(prompt.to_string());
            }
            Some("Generated summary [src/api/handler.rs:1].".to_string())
        };

        let _docs = generate_hierarchical_docs(&input, Some(&mut generator));
    }

    let prompt = architecture_prompts
        .iter()
        .find(|prompt| prompt.contains("Subsystem: src/api"))
        .expect("src/api architecture prompt");

    assert!(prompt.contains("| Component |\n| --- |\n"));
    assert!(prompt.contains(&format!("| handle [function] ({handle_id}) |")));
    assert!(prompt.contains(&format!("| route [function] ({route_id}) |")));
    assert!(!prompt.contains(&format!("- {handle_id}\n")));
    assert!(!prompt.contains(&format!("- {route_id}\n")));
}

#[test]
fn architecture_page_renders_layered_narrative_and_child_module_levels() {
    let input = CodewikiInput {
        leading_chunks: std::collections::BTreeMap::new(),
        files: vec![
            "src/api/handler.rs".to_string(),
            "src/domain/service.rs".to_string(),
            "src/storage/repo.rs".to_string(),
            "src/storage/backend/disk.rs".to_string(),
        ],
        graph_edges: vec![
            CodewikiGraphEdge::import(
                test_component_id("src/api/handler.rs", "handle", "function"),
                test_component_id("src/domain/service.rs", "Service", "class"),
            ),
            CodewikiGraphEdge::import(
                test_component_id("src/domain/service.rs", "Service", "class"),
                test_component_id("src/storage/repo.rs", "Repo", "class"),
            ),
        ],
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![
            test_symbol(
                "src/api/handler.rs",
                "handle",
                "function",
                1,
                "pub fn handle()",
            ),
            test_symbol(
                "src/domain/service.rs",
                "Service",
                "class",
                1,
                "pub struct Service;",
            ),
            test_symbol(
                "src/storage/repo.rs",
                "Repo",
                "class",
                1,
                "pub struct Repo;",
            ),
            test_symbol(
                "src/storage/backend/disk.rs",
                "Disk",
                "class",
                1,
                "pub struct Disk;",
            ),
        ],
    };

    let mut narrative_prompts = Vec::new();
    let mut generator = |prompt: &str, system: &str, _tier: PromptTier| {
        if system == prompts::ARCHITECTURE_NARRATIVE_SYSTEM {
            narrative_prompts.push(prompt.to_string());
            Some(
                "The api layer sits atop domain, which persists through storage \
                 [src/api/handler.rs:1]."
                    .to_string(),
            )
        } else if system == prompts::ARCHITECTURE_SYSTEM {
            Some("Owns one layer of the system [src/api/handler.rs:1].".to_string())
        } else {
            None
        }
    };
    let docs = generate_hierarchical_docs(&input, Some(&mut generator));
    let docs_by_path = docs.into_iter().collect::<BTreeMap<_, _>>();
    let rendered = docs_by_path
        .get("code/_architecture.md")
        .expect("architecture overview doc");

    assert!(rendered.contains("## Overview"), "{rendered}");
    assert!(
        rendered.contains("The api layer sits atop domain"),
        "{rendered}"
    );
    // The subsystem-map mermaid diagram is gone; the narrative still draws on the
    // cross-subsystem dependency edges (asserted via the narrative prompt below).
    assert!(!rendered.contains("## Subsystem Map"), "{rendered}");
    assert!(!rendered.contains("```mermaid"), "{rendered}");
    // One module level is enumerated in the subsystem table.
    assert!(
        rendered.contains("[[code/modules/src/storage/backend\\|src/storage/backend]]"),
        "{rendered}"
    );

    let narrative_prompt = narrative_prompts
        .first()
        .expect("narrative prompt captured");
    assert!(
        narrative_prompt.contains("- src/api -> src/domain"),
        "{narrative_prompt}"
    );
    assert!(
        narrative_prompt.contains("- src/domain -> src/storage"),
        "{narrative_prompt}"
    );
}

#[test]
fn architecture_page_has_no_diagram_and_range_free_frontmatter_without_graph_edges() {
    let input = CodewikiInput {
        leading_chunks: std::collections::BTreeMap::new(),
        files: vec![
            "crates/gcode/src/lib.rs".to_string(),
            "crates/gcore/src/lib.rs".to_string(),
        ],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Unavailable,
        symbols: vec![
            test_symbol(
                "crates/gcode/src/lib.rs",
                "Gcode",
                "class",
                1,
                "pub struct Gcode;",
            ),
            test_symbol(
                "crates/gcore/src/lib.rs",
                "Gcore",
                "class",
                1,
                "pub struct Gcore;",
            ),
        ],
    };

    let docs = generate_hierarchical_docs(&input, None);
    let docs_by_path = docs.into_iter().collect::<BTreeMap<_, _>>();
    let rendered = docs_by_path
        .get("code/_architecture.md")
        .expect("architecture overview doc");
    let yaml = rendered
        .strip_prefix("---\n")
        .and_then(|content| content.split_once("---\n\n"))
        .map(|(yaml, _)| yaml)
        .expect("frontmatter block");
    let frontmatter: serde_yaml::Value = serde_yaml::from_str(yaml).expect("parse frontmatter");
    let provenance = frontmatter
        .get("provenance")
        .and_then(serde_yaml::Value::as_sequence)
        .expect("provenance entries");

    // No mermaid diagram is emitted (the structural fallback diagram is gone),
    // and graph unavailability does not degrade the page.
    assert!(!rendered.contains("## Subsystem Map"), "{rendered}");
    assert!(!rendered.contains("```mermaid"), "{rendered}");
    assert!(!rendered.contains("graph-unavailable"), "{rendered}");
    assert!(provenance.iter().all(|entry| entry.get("file").is_some()));
    assert!(provenance.iter().all(|entry| entry.get("ranges").is_none()));
}

fn inline_marker_count(text: &str) -> usize {
    text.split_whitespace()
        .filter(|token| {
            token
                .strip_prefix('[')
                .and_then(|value| value.strip_suffix(']'))
                .is_some_and(|value| value.chars().all(|ch| ch.is_ascii_digit()))
        })
        .count()
}

// --- Model-seeded architecture diagrams (#891) -----------------------------

/// A minimal CodewikiInput with two files in distinct subsystems so the
/// architecture page is built. Diagrams come from the SystemModel, not these.
fn diagram_input() -> CodewikiInput {
    CodewikiInput {
        leading_chunks: std::collections::BTreeMap::new(),
        files: vec![
            "crates/gcode/src/lib.rs".to_string(),
            "crates/gcore/src/lib.rs".to_string(),
        ],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![
            test_symbol(
                "crates/gcode/src/lib.rs",
                "Code",
                "class",
                1,
                "pub struct Code;",
            ),
            test_symbol(
                "crates/gcore/src/lib.rs",
                "Core",
                "class",
                1,
                "pub struct Core;",
            ),
        ],
    }
}

/// A realistic three-binary + foundation workspace model.
fn diagram_model() -> SystemModel {
    let krate = |name: &str, path: &str, bin: bool, lib: bool| Crate {
        name: name.to_string(),
        path: path.to_string(),
        is_binary: bin,
        is_lib: lib,
    };
    let edge = |from: &str, to: &str| Edge {
        from: from.to_string(),
        to: to.to_string(),
    };
    let boundary = |name: &str, kind: ServiceKind, pulled: &[&str]| ServiceBoundary {
        name: name.to_string(),
        kind,
        pulled_in_by: pulled.iter().map(|s| s.to_string()).collect(),
    };
    let mut features_by_crate = std::collections::BTreeMap::new();
    features_by_crate.insert(
        "gobby-code".to_string(),
        vec!["ai".to_string(), "postgres".to_string()],
    );
    SystemModel {
        crates: vec![
            krate("gobby-code", "crates/gcode", true, false),
            krate("gobby-core", "crates/gcore", false, true),
            krate("gobby-hooks", "crates/ghook", true, false),
        ],
        edges: vec![
            edge("gobby-code", "gobby-core"),
            edge("gobby-hooks", "gobby-core"),
        ],
        services: vec![
            boundary(
                "PostgreSQL hub",
                ServiceKind::Postgres,
                &["gobby-code (feature: postgres)"],
            ),
            boundary(
                "Embedding API",
                ServiceKind::EmbeddingApi,
                &["gobby-code (feature: ai)"],
            ),
            boundary(
                "Gobby daemon",
                ServiceKind::Daemon,
                &["gobby-code (feature: ai)"],
            ),
            boundary(
                "ghook inbox",
                ServiceKind::GhookInbox,
                &["gobby-hooks (always)"],
            ),
        ],
        runtime_modes: vec![RuntimeMode::Standalone, RuntimeMode::DaemonAttached],
        features_by_crate,
        notes: Vec::new(),
    }
}

/// Drive the core generator with an explicit system model (AI off) and return
/// the architecture page's BuiltDoc.
fn build_architecture_page(model: Option<&SystemModel>) -> BuiltDoc {
    let input = diagram_input();
    let mut generate = None::<&mut TextGenerator<'_>>;
    let mut progress = CodewikiProgress::silent();
    let doc_scope = DocPruneScope::unscoped();
    let mut docs = Vec::new();
    generate_hierarchical_docs_core(
        &input,
        None,
        model,
        None,
        None,
        &mut generate,
        &mut None,
        &mut None,
        AiDepth::Symbols,
        CodewikiAiOutcome::default(),
        &mut None,
        &mut progress,
        &doc_scope,
        &mut |doc| {
            docs.push(doc);
            Ok(())
        },
    )
    .expect("generate docs");
    docs.into_iter()
        .find(|doc| doc.path == "code/_architecture.md")
        .expect("architecture page built")
}

#[test]
fn architecture_page_renders_model_seeded_diagrams_without_degrading() {
    let model = diagram_model();
    let page = build_architecture_page(Some(&model));

    // The diagram section is present with at least the topology + one flow.
    assert!(
        page.content.contains("## Architecture Diagrams"),
        "missing diagram section:\n{}",
        page.content
    );
    assert!(page.content.contains("```mermaid"), "{}", page.content);
    assert!(page.content.contains("flowchart TD"), "{}", page.content);
    assert!(page.content.contains("sequenceDiagram"), "{}", page.content);

    // Crate nodes and the gobby-code -> gobby-core edge are drawn.
    assert!(page.content.contains("gobby-code"));
    assert!(page.content.contains("gobby-core"));

    // Every emitted mermaid fence is individually well-formed per the gate.
    for block in mermaid_blocks(&page.content) {
        assert!(is_valid_mermaid(&block), "invalid fence emitted:\n{block}");
    }

    // AI is off (structural intent), so the page is NOT degraded; rendering
    // diagrams must never set degraded.
    assert!(!page.degraded, "diagram render must not degrade the page");
    assert!(!page.content.contains("degraded: true"), "{}", page.content);
}

#[test]
fn sparse_model_omits_diagrams_but_preserves_page_and_does_not_degrade() {
    // A fully-partial model (failed manifest read): zero crates, zero services.
    let empty = SystemModel {
        crates: Vec::new(),
        edges: Vec::new(),
        services: Vec::new(),
        runtime_modes: vec![RuntimeMode::Standalone, RuntimeMode::DaemonAttached],
        features_by_crate: std::collections::BTreeMap::new(),
        notes: vec!["cannot read workspace manifest".to_string()],
    };
    let page = build_architecture_page(Some(&empty));

    // No diagram section and no mermaid fence — omission is normal.
    assert!(
        !page.content.contains("## Architecture Diagrams"),
        "{}",
        page.content
    );
    assert!(!page.content.contains("```mermaid"), "{}", page.content);

    // The rest of the page (heading, subsystems table) is preserved.
    assert!(
        page.content.contains("# Architecture Overview"),
        "{}",
        page.content
    );
    assert!(page.content.contains("## Subsystems"), "{}", page.content);

    // A missing diagram is NOT degradation.
    assert!(!page.degraded, "omitted diagram must not degrade the page");
    assert!(!page.content.contains("degraded: true"), "{}", page.content);

    // And passing no model at all behaves identically (no diagrams, no degrade).
    let no_model = build_architecture_page(None);
    assert!(!no_model.content.contains("## Architecture Diagrams"));
    assert!(!no_model.degraded);
}

/// Extract each ```` ```mermaid ```` ... ```` ``` ```` block (inclusive) from a
/// rendered page so it can be re-validated.
fn mermaid_blocks(page: &str) -> Vec<String> {
    let mut blocks = Vec::new();
    let lines: Vec<&str> = page.lines().collect();
    let mut i = 0;
    while i < lines.len() {
        if lines[i].trim() == "```mermaid" {
            let start = i;
            let mut j = i + 1;
            while j < lines.len() && lines[j].trim() != "```" {
                j += 1;
            }
            assert!(
                j < lines.len(),
                "unterminated mermaid fence starting at rendered line {}",
                start + 1
            );
            let mut block = lines[start..=j].join("\n");
            block.push('\n');
            blocks.push(block);
            i = j + 1;
            continue;
        }
        i += 1;
    }
    blocks
}
