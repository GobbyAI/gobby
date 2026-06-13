use super::*;
use crate::explainer::{ExplainerPrompt, ExplainerResponse};
use crate::provenance::ProvenanceGraph;
use crate::session::{AcceptedResearchNote, ResearchScope, ResearchSession};
use crate::synthesis::SynthesizedPage;

fn session_with_note(scope: &ResearchScope, title: &str, relative_path: &str) -> ResearchSession {
    ResearchSession {
        session_id: "research-compile-test".to_string(),
        question: "How should compile handoff work?".to_string(),
        prompt: "Compile source-grounded research".to_string(),
        scope: scope.clone(),
        source_constraints: vec!["accepted notes only".to_string()],
        agent_count: 1,
        dispatch_task_id: Some("#302".to_string()),
        dispatch: None,
        accepted_notes: vec![AcceptedResearchNote {
            title: title.to_string(),
            path: scope.root().join(relative_path),
            code_citations: Vec::new(),
            degradation: None,
        }],
        compile_state: None,
    }
}

#[test]
fn compile_bundle_contains_required_sections() {
    let temp = tempfile::tempdir().expect("tempdir");
    let scope = ResearchScope::project_for_id("project-1", temp.path());
    let note_path = scope.root().join("raw/research/compile.md");
    std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
    std::fs::write(
        &note_path,
        "---\ntitle: Compile behavior\nsource: daemon notes\n---\n\nCitation: Example Docs, Compile API\nConflict: Workers disagree about overwrite behavior.\nGap: Missing benchmark evidence.\nAccepted chunk about durable synthesis handoff.",
    )
    .expect("note written");
    let mut session = session_with_note(&scope, "Compile behavior", "raw/research/compile.md");

    let outcome = prepare_handoff(
        &mut session,
        CompileRequest {
            topic: "Compile behavior".to_string(),
            outline: vec![
                "Durable handoff".to_string(),
                "Synthesis inputs".to_string(),
            ],
            target_page: Some(PathBuf::from("compile-behavior.md")),
            write_intent: false,
        },
    )
    .expect("compile handoff prepared");

    assert_eq!(outcome.bundle.outline.len(), 2);
    assert_eq!(outcome.bundle.accepted_sources.len(), 1);
    assert_eq!(outcome.bundle.citations, vec!["Example Docs, Compile API"]);
    assert_eq!(
        outcome.bundle.conflicting_claims,
        vec!["Workers disagree about overwrite behavior."]
    );
    assert_eq!(
        outcome.bundle.missing_evidence,
        vec!["Missing benchmark evidence."]
    );

    let rendered = std::fs::read_to_string(&outcome.bundle.path).expect("bundle written");
    assert!(rendered.contains("## Topic outline"));
    assert!(rendered.contains("## Accepted sources"));
    assert!(rendered.contains("## Citations"));
    assert!(rendered.contains("## Conflicting claims"));
    assert!(rendered.contains("## Missing evidence"));
}

#[test]
fn compile_handoff_is_non_destructive_by_default() {
    let temp = tempfile::tempdir().expect("tempdir");
    let scope = ResearchScope::project_for_id("project-1", temp.path());
    let page_path = scope.root().join("compile-behavior.md");
    std::fs::write(&page_path, "human-authored wiki page").expect("page written");
    let note_path = scope.root().join("raw/research/compile.md");
    std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
    std::fs::write(&note_path, "Citation: Example Docs").expect("note written");
    let mut session = session_with_note(&scope, "Compile behavior", "raw/research/compile.md");

    let outcome = prepare_handoff(
        &mut session,
        CompileRequest {
            topic: "Compile behavior".to_string(),
            outline: vec!["Durable handoff".to_string()],
            target_page: Some(PathBuf::from("compile-behavior.md")),
            write_intent: false,
        },
    )
    .expect("compile handoff prepared");

    assert_eq!(
        std::fs::read_to_string(&page_path).expect("page retained"),
        "human-authored wiki page"
    );
    assert_ne!(outcome.bundle.path, page_path);
    assert!(!outcome.state.write_intent);
}

#[test]
fn prepare_handoff_does_not_write_target_page() {
    let temp = tempfile::tempdir().expect("tempdir");
    let scope = ResearchScope::project_for_id("project-1", temp.path());
    let page_path = scope.root().join("compile-behavior.md");
    std::fs::write(&page_path, "human-authored wiki page").expect("page written");
    let note_path = scope.root().join("raw/research/compile.md");
    std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
    std::fs::write(&note_path, "Citation: Example Docs").expect("note written");
    let mut session = session_with_note(&scope, "Compile behavior", "raw/research/compile.md");

    let outcome = prepare_handoff(
        &mut session,
        CompileRequest {
            topic: "Compile behavior".to_string(),
            outline: vec!["Durable handoff".to_string()],
            target_page: Some(PathBuf::from("compile-behavior.md")),
            write_intent: true,
        },
    )
    .expect("compile handoff prepared");

    assert_eq!(
        std::fs::read_to_string(&page_path).expect("page retained"),
        "human-authored wiki page"
    );
    assert!(outcome.state.write_intent);
}

#[test]
fn compile_fails_on_out_of_scope_accepted_note() {
    let in_scope = tempfile::tempdir().expect("in scope tempdir");
    let out_of_scope = tempfile::tempdir().expect("out of scope tempdir");
    let scope = ResearchScope::project_for_id("project-1", in_scope.path());
    let in_scope_path = scope.root().join("raw/research/in-scope.md");
    std::fs::create_dir_all(in_scope_path.parent().expect("note parent")).expect("raw dir");
    std::fs::write(&in_scope_path, "Citation: In-scope citation").expect("note written");
    let mut session = session_with_note(&scope, "In scope", "raw/research/in-scope.md");
    session.accepted_notes.push(AcceptedResearchNote {
        title: "Out of scope".to_string(),
        path: out_of_scope.path().join("raw/research/out-of-scope.md"),
        code_citations: Vec::new(),
        degradation: None,
    });
    let out_path = out_of_scope.path().join("raw/research/out-of-scope.md");
    std::fs::create_dir_all(out_path.parent().expect("out parent")).expect("out raw dir");
    std::fs::write(&out_path, "Out of scope citation").expect("out note written");

    let err = prepare_handoff(
        &mut session,
        CompileRequest {
            topic: "Scoped compile".to_string(),
            outline: vec!["Scoped sources".to_string()],
            target_page: None,
            write_intent: false,
        },
    )
    .expect_err("out-of-scope accepted note must fail fast");

    assert!(matches!(
        err,
        WikiError::InvalidInput {
            field: "accepted_note",
            ..
        }
    ));
}

#[test]
fn compile_rejects_absolute_or_escaping_target_pages() {
    let temp = tempfile::tempdir().expect("tempdir");
    let scope = ResearchScope::project_for_id("project-1", temp.path());
    let note_path = scope.root().join("raw/research/compile.md");
    std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
    std::fs::write(&note_path, "Citation: Example Docs").expect("note written");
    let mut absolute_session =
        session_with_note(&scope, "Compile behavior", "raw/research/compile.md");

    let absolute = prepare_handoff(
        &mut absolute_session,
        CompileRequest {
            topic: "Compile behavior".to_string(),
            outline: vec!["Overview".to_string()],
            target_page: Some(scope.root().join("absolute.md")),
            write_intent: false,
        },
    )
    .expect_err("absolute target page must be rejected");
    assert!(matches!(
        absolute,
        WikiError::InvalidInput {
            field: "target_page",
            ..
        }
    ));

    let mut escaping_session =
        session_with_note(&scope, "Compile behavior", "raw/research/compile.md");
    let escaping = prepare_handoff(
        &mut escaping_session,
        CompileRequest {
            topic: "Compile behavior".to_string(),
            outline: vec!["Overview".to_string()],
            target_page: Some(PathBuf::from("../outside.md")),
            write_intent: false,
        },
    )
    .expect_err("escaping target page must be rejected");
    assert!(matches!(
        escaping,
        WikiError::InvalidInput {
            field: "target_page",
            ..
        }
    ));
}

#[cfg(unix)]
#[test]
fn compile_rejects_target_page_through_symlinked_parent() {
    let vault = tempfile::tempdir().expect("vault tempdir");
    let outside = tempfile::tempdir().expect("outside tempdir");
    std::os::unix::fs::symlink(outside.path(), vault.path().join("linked"))
        .expect("symlink outside");

    let error = write_target_page(
        vault.path(),
        &vault.path().join("linked/outside.md"),
        "# Outside\n",
    )
    .expect_err("symlinked target parent rejected");

    assert!(matches!(
        error,
        WikiError::InvalidInput {
            field: "target_page",
            ..
        }
    ));
}

#[cfg(windows)]
#[test]
fn compile_rejects_target_page_through_symlinked_parent() {
    let vault = tempfile::tempdir().expect("vault tempdir");
    let outside = tempfile::tempdir().expect("outside tempdir");
    if let Err(error) =
        std::os::windows::fs::symlink_dir(outside.path(), vault.path().join("linked"))
    {
        if matches!(
            error.kind(),
            std::io::ErrorKind::PermissionDenied | std::io::ErrorKind::Unsupported
        ) {
            eprintln!("skipping Windows symlink assertion: {error}");
            return;
        }
        panic!("symlink outside: {error}");
    }

    let error = write_target_page(
        vault.path(),
        &vault.path().join("linked/outside.md"),
        "# Outside\n",
    )
    .expect_err("symlinked target parent rejected");

    assert!(matches!(
        error,
        WikiError::InvalidInput {
            field: "target_page",
            ..
        }
    ));
}

#[test]
fn compile_writes_obsidian_markdown() {
    let temp = tempfile::tempdir().expect("tempdir");
    let scope = ResearchScope::project_for_id("project-1", temp.path());
    let note_path = scope.root().join("raw/research/compile.md");
    std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
    std::fs::write(
        &note_path,
        concat!(
            "---\n",
            "title: Compile behavior\n",
            "source: daemon notes\n",
            "---\n\n",
            "Citation: Example Docs, Compile API\n",
            "Compile turns accepted notes into source-grounded wiki articles.\n",
            "Evidence sections keep claims traceable to their matching outline entries."
        ),
    )
    .expect("note written");
    let mut session = session_with_note(&scope, "Compile behavior", "raw/research/compile.md");

    let outcome = compile_to_wiki(
        &mut session,
        CompileRequest {
            topic: "Durable Compile".to_string(),
            outline: vec!["Overview".to_string(), "Evidence".to_string()],
            target_page: None,
            write_intent: false,
        },
    )
    .expect("wiki articles compiled");

    let page = std::fs::read_to_string(&outcome.article_path).expect("article written");
    assert!(
        outcome
            .article_path
            .ends_with("knowledge/topics/durable-compile.md")
    );
    assert!(page.starts_with("---\n"));
    assert!(page.contains("title: \"Durable Compile\""));
    assert!(page.contains("source_kind: \"topic\""));
    assert!(page.contains("[[knowledge/sources/compile-behavior|Compile behavior]]"));
    assert!(page.contains("Example Docs, Compile API"));

    let source_page = scope.root().join("knowledge/sources/compile-behavior.md");
    assert!(source_page.exists());
    let provenance =
        std::fs::read_to_string(scope.root().join("meta/provenance.json")).expect("provenance");
    assert!(provenance.contains("knowledge/topics/durable-compile.md"));
    assert!(provenance.contains("raw/research/compile.md"));
    let provenance = ProvenanceGraph::load_from_vault(scope.root()).expect("load provenance graph");
    let links = provenance.links();
    assert_eq!(links.len(), 2);
    assert_eq!(links[0].section.section_id, "durable-compile");
    assert_eq!(links[1].section.section_id, "evidence");
    let article_page = std::path::Path::new("knowledge/topics/durable-compile.md");
    assert_eq!(
        provenance
            .links_for_page_section(article_page, "durable-compile")
            .len(),
        1
    );
    assert_eq!(
        provenance
            .links_for_page_section(article_page, "evidence")
            .len(),
        1
    );
    let source = &provenance.links()[0].source;
    assert!(source.byte_end > source.byte_start);
}

#[test]
fn index_update_preserves_unrelated_entries() {
    let temp = tempfile::tempdir().expect("tempdir");
    let scope = ResearchScope::project_for_id("project-1", temp.path());
    std::fs::write(
        scope.root().join("_index.md"),
        "# Wiki Index\n\n- [[knowledge/topics/existing|Existing Entry]]\n",
    )
    .expect("index written");
    let note_path = scope.root().join("raw/research/index.md");
    std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
    std::fs::write(&note_path, "Index updates keep unrelated entries.").expect("note written");
    let mut session = session_with_note(&scope, "Index behavior", "raw/research/index.md");

    compile_to_wiki(
        &mut session,
        CompileRequest {
            topic: "Index Preservation".to_string(),
            outline: vec!["Overview".to_string()],
            target_page: None,
            write_intent: false,
        },
    )
    .expect("wiki article compiled");

    let index = std::fs::read_to_string(scope.root().join("_index.md")).expect("index read");
    assert!(index.contains("[[knowledge/topics/existing|Existing Entry]]"));
    assert!(index.contains("[[knowledge/topics/index-preservation|Index Preservation]]"));
}

#[test]
fn index_update_uses_structural_heading_and_link_checks() {
    let temp = tempfile::tempdir().expect("tempdir");
    let article = SynthesizedPage {
        path: temp.path().join("knowledge/topics/exact.md"),
        title: "Exact".to_string(),
        markdown: "# Exact\n\n".to_string(),
        explainer: None,
    };
    std::fs::write(
        temp.path().join("_index.md"),
        concat!(
            "# Wiki Index\n\n",
            "## Compiled pages archive\n\n",
            "- [[knowledge/topics/exact|Exact]] archived copy\n"
        ),
    )
    .expect("index written");

    update_wiki_index(temp.path(), &article).expect("index updated");

    let index = std::fs::read_to_string(temp.path().join("_index.md")).expect("index read");
    assert!(index.lines().any(|line| line == "## Compiled pages"));
    assert_eq!(
        index
            .lines()
            .filter(|line| *line == "- [[knowledge/topics/exact|Exact]]")
            .count(),
        1
    );
}

#[test]
fn insert_compiled_page_link_creates_missing_compiled_heading() {
    let mut index = "# Wiki Index\n\n".to_string();

    insert_compiled_page_link(&mut index, "[[knowledge/topics/missing|Missing]]")
        .expect("missing heading is created");

    assert!(index.contains("## Compiled pages\n\n- [[knowledge/topics/missing|Missing]]\n"));
}

#[test]
fn write_target_page_rejects_existing_page_without_overwrite_race() {
    let vault = tempfile::tempdir().expect("vault tempdir");
    let target = vault.path().join("existing.md");
    std::fs::write(&target, "human-authored wiki page").expect("existing page");

    let error = write_target_page(vault.path(), &target, "# Replacement\n")
        .expect_err("existing target rejected");

    assert!(matches!(
        error,
        WikiError::InvalidInput {
            field: "write_intent",
            ..
        }
    ));
    assert_eq!(
        std::fs::read_to_string(&target).expect("existing page retained"),
        "human-authored wiki page"
    );
}

#[test]
fn compile_explainer_generates_grounded_prose_sections() {
    let temp = tempfile::tempdir().expect("tempdir");
    let scope = ResearchScope::project_for_id("project-1", temp.path());
    let note_path = scope.root().join("raw/research/compile.md");
    std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
    std::fs::write(
        &note_path,
        "---\ntitle: Compile behavior\n---\n\nCompile turns accepted notes into grounded articles.",
    )
    .expect("note written");
    let mut session = session_with_note(&scope, "Compile behavior", "raw/research/compile.md");

    let mut prompts = Vec::new();
    let outcome = {
        let mut generator = |prompt: &ExplainerPrompt| {
            prompts.push(prompt.user.clone());
            Ok(ExplainerResponse {
                text: "## Overview\nCompile grounds articles in accepted notes \
                       [source: raw/research/compile.md]. It never keeps invented citations \
                       [source: raw/research/invented.md].\n"
                    .to_string(),
                model: Some("mock-model".to_string()),
                route: "daemon",
            })
        };
        compile_to_wiki_with_options(
            &mut session,
            CompileRequest {
                topic: "Durable Compile".to_string(),
                outline: vec!["Overview".to_string()],
                target_page: None,
                write_intent: false,
            },
            WikiCompileOptions::default(),
            Some(&mut generator),
        )
        .expect("wiki article compiled")
    };

    let page = std::fs::read_to_string(&outcome.article_path).expect("article written");
    assert!(page.contains("synthesis_mode: \"daemon\""), "{page}");
    assert!(!page.contains("degraded:"), "{page}");
    assert!(page.contains("## Overview"), "{page}");
    assert!(
        page.contains("accepted notes [[knowledge/sources/compile-behavior|Compile behavior]]."),
        "{page}"
    );
    assert!(!page.contains("[source:"), "{page}");
    assert!(!page.contains("invented.md"), "{page}");

    let report = outcome.explainer.expect("explainer report");
    assert_eq!(report.status, "generated");
    assert_eq!(report.route, Some("daemon"));
    assert_eq!(report.model.as_deref(), Some("mock-model"));
    assert_eq!(report.citations_kept, 1);
    assert_eq!(report.citations_stripped, 1);

    assert!(outcome.prompt.tokens_estimated > 0);
    assert_eq!(outcome.prompt.truncated_sources, 0);
    let prompt_user = prompts.first().expect("explainer prompt captured");
    assert!(
        prompt_user.contains("[source: raw/research/compile.md]"),
        "{prompt_user}"
    );
    assert!(
        prompt_user.contains("Compile turns accepted notes"),
        "{prompt_user}"
    );
}

#[test]
fn compile_explainer_failure_degrades_and_keeps_structural_skeleton() {
    let temp = tempfile::tempdir().expect("tempdir");
    let scope = ResearchScope::project_for_id("project-1", temp.path());
    let note_path = scope.root().join("raw/research/compile.md");
    std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
    std::fs::write(&note_path, "Accepted compile evidence.").expect("note written");
    let mut session = session_with_note(&scope, "Compile behavior", "raw/research/compile.md");

    let mut generator = |_prompt: &ExplainerPrompt| {
        Err::<ExplainerResponse, _>("text lane unavailable".to_string())
    };
    let outcome = compile_to_wiki_with_options(
        &mut session,
        CompileRequest {
            topic: "Degraded Compile".to_string(),
            outline: vec!["Overview".to_string()],
            target_page: None,
            write_intent: false,
        },
        WikiCompileOptions::default(),
        Some(&mut generator),
    )
    .expect("wiki article compiled despite explainer failure");

    let page = std::fs::read_to_string(&outcome.article_path).expect("article written");
    assert!(page.contains("synthesis_mode: \"fallback\""), "{page}");
    assert!(page.contains("degraded: true"), "{page}");
    assert!(page.contains("degraded_sources:"), "{page}");
    assert!(page.contains("  - model_provider_unavailable"), "{page}");
    assert!(page.contains("## Overview"), "{page}");

    let report = outcome.explainer.expect("explainer report");
    assert_eq!(report.status, "failed");
    assert_eq!(report.error.as_deref(), Some("text lane unavailable"));
    assert_eq!(report.citations_kept, 0);
}

#[test]
fn compile_without_generator_stays_structural_without_degradation() {
    let temp = tempfile::tempdir().expect("tempdir");
    let scope = ResearchScope::project_for_id("project-1", temp.path());
    let note_path = scope.root().join("raw/research/compile.md");
    std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
    std::fs::write(&note_path, "Accepted compile evidence.").expect("note written");
    let mut session = session_with_note(&scope, "Compile behavior", "raw/research/compile.md");

    let outcome = compile_to_wiki_with_options(
        &mut session,
        CompileRequest {
            topic: "Structural Compile".to_string(),
            outline: vec!["Overview".to_string()],
            target_page: None,
            write_intent: false,
        },
        WikiCompileOptions::default(),
        None,
    )
    .expect("wiki article compiled");

    let page = std::fs::read_to_string(&outcome.article_path).expect("article written");
    assert!(page.contains("synthesis_mode: \"fallback\""), "{page}");
    assert!(!page.contains("degraded:"), "{page}");
    assert!(page.contains("## Overview"), "{page}");

    let report = outcome.explainer.expect("explainer report");
    assert_eq!(report.status, "skipped");
}
