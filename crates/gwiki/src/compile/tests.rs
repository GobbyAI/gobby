use super::*;
use gobby_core::ai_types::TokenUsage;

use crate::explainer::{ExplainerPrompt, ExplainerResponse};
use crate::provenance::ProvenanceGraph;
use crate::session::{AcceptedResearchNote, ResearchScope, ResearchSession};
use crate::sources::{SourceDraft, SourceKind, SourceManifest};

/// Content pages in `directory`, sorted, ignoring the `_context.md`
/// navigation file that catalog regeneration adds alongside them (#17730).
fn content_page_names(directory: &std::path::Path) -> Vec<String> {
    let mut names: Vec<String> = std::fs::read_dir(directory)
        .expect("directory listed")
        .filter_map(Result::ok)
        .map(|entry| entry.file_name().to_string_lossy().into_owned())
        .filter(|name| name != "_context.md")
        .collect();
    names.sort();
    names
}

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
    assert!(
        rendered.contains("# Compile bundle: Compile behavior"),
        "{rendered}"
    );
    assert!(
        rendered.contains("## Target page\n\n- compile-behavior.md"),
        "{rendered}"
    );
    assert!(
        rendered.contains("## Write intent\n\n- false"),
        "{rendered}"
    );
    assert!(rendered.contains("raw/research/compile.md"), "{rendered}");
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

    let error = normalize_target_page(
        vault.path(),
        Some(std::path::Path::new("linked/outside.md")),
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

    let error = normalize_target_page(
        vault.path(),
        Some(std::path::Path::new("linked/outside.md")),
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
    let note = concat!(
        "---\n",
        "title: Compile behavior\n",
        "source: daemon notes\n",
        "---\n\n",
        "Citation: Example Docs, Compile API\n",
        "Compile turns accepted notes into source-grounded wiki articles.\n",
        "Evidence sections keep claims traceable to their matching outline entries."
    );
    std::fs::write(&note_path, note).expect("note written");
    let source_hash = gobby_core::indexing::content_hash(note.as_bytes());
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
    assert!(page.contains(&format!(
        "content_hash: {}",
        crate::page_version::content_hash(&page)
    )));
    assert!(page.contains(&format!("compiled_from:\n  - {source_hash}\n")));
    assert!(page.contains("[[knowledge/sources/compile-behavior|Compile behavior]]"));
    assert!(page.contains("Example Docs, Compile API"));

    let source_page = scope.root().join("knowledge/sources/compile-behavior.md");
    assert!(source_page.exists());
    let source_page = std::fs::read_to_string(source_page).expect("source page");
    assert!(source_page.contains(&format!(
        "content_hash: {}",
        crate::page_version::content_hash(&source_page)
    )));
    assert!(source_page.contains(&format!("compiled_from:\n  - {source_hash}\n")));
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
    assert_eq!(source.source_hash, source_hash);
    assert_eq!(
        provenance.links()[0].section.content_hash,
        crate::page_version::content_hash(&page)
    );
}

#[test]
fn unchanged_recompile_preserves_body_hash_across_new_handoff() {
    let temp = tempfile::tempdir().expect("tempdir");
    let scope = ResearchScope::project_for_id("project-1", temp.path());
    let note_path = scope.root().join("raw/research/compile.md");
    std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
    std::fs::write(&note_path, "Stable compile evidence.\n").expect("note written");
    let request = || CompileRequest {
        topic: "Stable Compile".to_string(),
        outline: vec!["Overview".to_string()],
        target_page: None,
        write_intent: true,
    };

    let mut first_session =
        session_with_note(&scope, "Compile behavior", "raw/research/compile.md");
    let first = compile_to_wiki(&mut first_session, request()).expect("first compile");
    let first_page = std::fs::read_to_string(&first.article_path).expect("first page");

    let mut second_session =
        session_with_note(&scope, "Compile behavior", "raw/research/compile.md");
    let second = compile_to_wiki(&mut second_session, request()).expect("second compile");
    let second_page = std::fs::read_to_string(&second.article_path).expect("second page");

    assert_ne!(first.handoff_id, second.handoff_id);
    assert_eq!(
        crate::page_version::content_hash(&first_page),
        crate::page_version::content_hash(&second_page)
    );
}

#[test]
fn compile_reuses_existing_source_digest_pages() {
    let temp = tempfile::tempdir().expect("tempdir");
    let scope = ResearchScope::project_for_id("project-1", temp.path());
    let note_body = "Compile turns accepted notes into source-grounded wiki articles.\n";
    let note_path = scope.root().join("raw/research/compile.md");
    std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
    std::fs::write(&note_path, note_body).expect("note written");
    // Register the note as a manifest source and materialize its digest page,
    // the way session/document ingest does.
    let record = SourceManifest::register(
        scope.root(),
        SourceDraft::new(
            "raw/research/compile.md",
            SourceKind::Text,
            "2026-07-05T00:00:00Z",
            note_body.as_bytes().to_vec(),
        ),
    )
    .expect("source registered");
    let digest_path = scope
        .root()
        .join("knowledge/sources")
        .join(format!("{}.md", record.id));
    std::fs::create_dir_all(digest_path.parent().expect("digest parent")).expect("sources dir");
    std::fs::write(
        &digest_path,
        "---\ntitle: Compile behavior\n---\n\nRich digest body.\n",
    )
    .expect("digest written");
    let mut session = session_with_note(&scope, "Compile behavior", "raw/research/compile.md");

    let outcome = compile_to_wiki(
        &mut session,
        CompileRequest {
            topic: "Durable Compile".to_string(),
            outline: vec!["Overview".to_string()],
            target_page: None,
            write_intent: false,
        },
    )
    .expect("wiki articles compiled");

    // The digest is the only page under knowledge/sources/ — no duplicate stub.
    let entries = content_page_names(&scope.root().join("knowledge/sources"));
    assert_eq!(entries, vec![format!("{}.md", record.id)]);
    assert!(outcome.source_paths.is_empty());
    let article = std::fs::read_to_string(&outcome.article_path).expect("article written");
    assert!(
        article.contains(&format!(
            "[[knowledge/sources/{}|Compile behavior]]",
            record.id
        )),
        "{article}"
    );
    assert_eq!(
        std::fs::read_to_string(&digest_path).expect("digest retained"),
        "---\ntitle: Compile behavior\n---\n\nRich digest body.\n"
    );
}

#[test]
fn recompile_updates_source_stub_pages_in_place() {
    let temp = tempfile::tempdir().expect("tempdir");
    let scope = ResearchScope::project_for_id("project-1", temp.path());
    let note_path = scope.root().join("raw/research/compile.md");
    std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
    std::fs::write(&note_path, "First compile evidence.\n").expect("note written");
    let request = || CompileRequest {
        topic: "Durable Compile".to_string(),
        outline: vec!["Overview".to_string()],
        target_page: Some(PathBuf::from("knowledge/topics/durable-compile.md")),
        write_intent: true,
    };
    let mut session = session_with_note(&scope, "Compile behavior", "raw/research/compile.md");
    compile_to_wiki(&mut session, request()).expect("first compile succeeded");

    std::fs::write(&note_path, "Recompiled evidence.\n").expect("note rewritten");
    let mut session = session_with_note(&scope, "Compile behavior", "raw/research/compile.md");
    let outcome = compile_to_wiki(&mut session, request()).expect("recompile succeeded");

    // The recompile resolves the stub written by the first compile and
    // updates it in place — no slug-suffixed sibling pages (#17596).
    let entries = content_page_names(&scope.root().join("knowledge/sources"));
    assert_eq!(entries, vec!["compile-behavior.md".to_string()]);
    assert_eq!(
        outcome.source_paths,
        vec![scope.root().join("knowledge/sources/compile-behavior.md")]
    );
    let stub = std::fs::read_to_string(scope.root().join("knowledge/sources/compile-behavior.md"))
        .expect("stub read");
    assert!(stub.contains("Recompiled evidence."), "{stub}");
    assert!(
        stub.contains("source_path: \"raw/research/compile.md\""),
        "{stub}"
    );
}

#[test]
fn recompile_overwrites_source_stub_without_write_intent() {
    let temp = tempfile::tempdir().expect("tempdir");
    let scope = ResearchScope::project_for_id("project-1", temp.path());
    let note_path = scope.root().join("raw/research/shared.md");
    std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
    std::fs::write(&note_path, "Shared source evidence.\n").expect("note written");

    // A first topic compiles the source, minting its derived stub page.
    let mut first = session_with_note(&scope, "Shared Source", "raw/research/shared.md");
    compile_to_wiki(
        &mut first,
        CompileRequest {
            topic: "First Topic".to_string(),
            outline: vec!["Overview".to_string()],
            target_page: Some(PathBuf::from("knowledge/topics/first-topic.md")),
            write_intent: true,
        },
    )
    .expect("first compile succeeded");

    // A second, brand-new topic references the SAME source with no write
    // intent. Its article is create-only, but the shared source stub already
    // exists — the stub is a deterministic machine digest, so it must overwrite
    // in place rather than fail loud or mint a slug-suffixed sibling (#17707).
    let mut second = session_with_note(&scope, "Shared Source", "raw/research/shared.md");
    let outcome = compile_to_wiki(
        &mut second,
        CompileRequest {
            topic: "Second Topic".to_string(),
            outline: vec!["Overview".to_string()],
            target_page: Some(PathBuf::from("knowledge/topics/second-topic.md")),
            write_intent: false,
        },
    )
    .expect("second compile overwrites shared stub without write intent");

    let entries = content_page_names(&scope.root().join("knowledge/sources"));
    assert_eq!(entries, vec!["shared-source.md".to_string()]);
    assert_eq!(
        outcome.source_paths,
        vec![scope.root().join("knowledge/sources/shared-source.md")]
    );
}

#[test]
fn recompile_without_target_page_updates_article_in_place() {
    let temp = tempfile::tempdir().expect("tempdir");
    let scope = ResearchScope::project_for_id("project-1", temp.path());
    let note_path = scope.root().join("raw/research/compile.md");
    std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
    std::fs::write(&note_path, "First compile evidence.\n").expect("note written");
    let request = || CompileRequest {
        topic: "Durable Compile".to_string(),
        outline: vec!["Overview".to_string()],
        target_page: None,
        write_intent: true,
    };
    let mut session = session_with_note(&scope, "Compile behavior", "raw/research/compile.md");
    let first = compile_to_wiki(&mut session, request()).expect("first compile succeeded");
    let first_page = std::fs::read_to_string(&first.article_path).expect("first article");
    let first_hash = crate::page_version::content_hash(&first_page);
    assert_eq!(
        first.article_path,
        scope.root().join("knowledge/topics/durable-compile.md")
    );

    std::fs::write(&note_path, "Recompiled evidence.\n").expect("note rewritten");
    let mut session = session_with_note(&scope, "Compile behavior", "raw/research/compile.md");
    let outcome = compile_to_wiki(&mut session, request()).expect("recompile succeeded");
    let recompiled_page =
        std::fs::read_to_string(&outcome.article_path).expect("recompiled article");
    let recompiled_hash = crate::page_version::content_hash(&recompiled_page);

    // The recompile resolves the article written by the first compile and
    // updates it in place — no -2 suffixed sibling article (#17635).
    assert_eq!(outcome.article_path, first.article_path);
    let entries = content_page_names(&scope.root().join("knowledge/topics"));
    assert_eq!(entries, vec!["durable-compile.md".to_string()]);
    assert_ne!(first_hash, recompiled_hash);
    let provenance =
        ProvenanceGraph::load_from_vault(scope.root()).expect("load replaced provenance");
    assert_eq!(provenance.links().len(), 1);
    assert_eq!(provenance.links()[0].section.content_hash, recompiled_hash);
    assert_eq!(
        provenance.links()[0].source.source_hash,
        gobby_core::indexing::content_hash(b"Recompiled evidence.\n")
    );
    // Resolving the existing page also feeds its body into the synthesis
    // prompt as update-over-create context.
    assert!(
        outcome
            .prompt
            .user
            .contains("update it rather than starting over"),
        "{}",
        outcome.prompt.user
    );
}

#[test]
fn compile_after_recapture_emits_single_digest_without_suffix() {
    let temp = tempfile::tempdir().expect("tempdir");
    let scope = ResearchScope::project_for_id("project-1", temp.path());
    let vault_root = scope.root().to_path_buf();
    let source_path = vault_root.join("recaptured-note.md");

    let mut ai_source = gobby_core::config::EnvOnlySource;
    let mut ai_context = gobby_core::ai_context::AiContext::resolve(None, &mut ai_source);
    let options = crate::api::IngestFileOptions {
        no_ai: true,
        ..crate::api::IngestFileOptions::default()
    };
    options.apply_to_ai_context(&mut ai_context);

    let scope_identity = scope.identity();
    for (body, fetched_at) in [
        ("# Note\n\nFirst capture body.\n", "2026-07-01T00:00:00Z"),
        (
            "# Note\n\nSecond capture body, changed.\n",
            "2026-07-02T00:00:00Z",
        ),
    ] {
        std::fs::write(&source_path, body).expect("write source");
        let mut store = crate::store::FakeWikiStore::default();
        crate::ingest::file::ingest_path(
            &vault_root,
            &mut store,
            &scope_identity,
            &ai_context,
            &options,
            crate::ingest::file::LocalFileSnapshot {
                path: &source_path,
                fetched_at,
            },
            &mut crate::progress::ProgressOptions::default(),
        )
        .expect("ingest capture");
    }

    // Re-capturing the changed file supersedes the first record (#17644), so
    // the compile input holds a single raw capture for the location.
    let raw_notes: Vec<PathBuf> = std::fs::read_dir(vault_root.join("raw"))
        .expect("raw dir")
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| {
            path.extension().is_some_and(|ext| ext == "md")
                && path.file_name().is_some_and(|name| name != "INDEX.md")
        })
        .collect();
    assert_eq!(raw_notes.len(), 1, "re-capture keeps a single raw source");

    let mut session = session_with_note(&scope, "Recaptured note", "raw/research/unused.md");
    session.accepted_notes = raw_notes
        .iter()
        .map(|path| AcceptedResearchNote {
            title: "Recaptured note".to_string(),
            path: path.clone(),
            code_citations: Vec::new(),
            degradation: None,
        })
        .collect();

    compile_to_wiki(
        &mut session,
        CompileRequest {
            topic: "Recaptured Note Topic".to_string(),
            outline: vec!["Overview".to_string()],
            target_page: Some(PathBuf::from("knowledge/topics/recaptured-note.md")),
            write_intent: true,
        },
    )
    .expect("compile succeeded");

    let digests = content_page_names(&vault_root.join("knowledge/sources"));
    assert_eq!(
        digests.len(),
        1,
        "single digest page, no -2 sibling: {digests:?}"
    );
    assert!(!digests[0].ends_with("-2.md"), "{digests:?}");
}

#[test]
fn recompile_of_machine_page_overwrites_without_write_intent() {
    let temp = tempfile::tempdir().expect("tempdir");
    let scope = ResearchScope::project_for_id("project-1", temp.path());
    let note_path = scope.root().join("raw/research/compile.md");
    std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
    std::fs::write(&note_path, "First compile evidence.\n").expect("note written");
    let request = |write_intent: bool| CompileRequest {
        topic: "Durable Compile".to_string(),
        outline: vec!["Overview".to_string()],
        target_page: None,
        write_intent,
    };

    // The first compile authors the machine article; it carries `synthesis_mode`.
    let mut session = session_with_note(&scope, "Compile behavior", "raw/research/compile.md");
    let first = compile_to_wiki(&mut session, request(true)).expect("first compile succeeded");
    assert!(
        std::fs::read_to_string(&first.article_path)
            .expect("article written")
            .contains("synthesis_mode:"),
        "first compile marks the page machine-owned"
    );

    // A recompile with no write intent now refreshes the machine-owned page in
    // place: the page's `synthesis_mode` provenance authorizes the overwrite, so
    // an automated recompile can self-drain re-fetched sources instead of failing
    // loud or minting a slug-suffixed sibling (#17708; supersedes the blanket
    // #17635 fail-loud for machine-owned pages).
    let mut session = session_with_note(&scope, "Compile behavior", "raw/research/compile.md");
    let second = compile_to_wiki(&mut session, request(false))
        .expect("recompile of a machine-owned page overwrites without write intent");
    assert_eq!(second.article_path, first.article_path);

    let articles: Vec<String> =
        content_page_names(first.article_path.parent().expect("article parent"))
            .into_iter()
            .filter(|name| name.ends_with(".md"))
            .collect();
    assert_eq!(articles.len(), 1, "no slug-suffixed sibling: {articles:?}");
}

#[test]
fn explicit_target_with_different_title_is_never_overwritten() {
    let temp = tempfile::tempdir().expect("tempdir");
    let scope = ResearchScope::project_for_id("project-1", temp.path());
    let note_path = scope.root().join("raw/research/compile.md");
    std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
    std::fs::write(&note_path, "Compile evidence.\n").expect("note written");
    let target = PathBuf::from("knowledge/topics/unrelated.md");
    let page_path = scope.root().join(&target);
    std::fs::create_dir_all(page_path.parent().expect("target parent")).expect("target dir");
    let original = "---\ntitle: Unrelated Topic\nsynthesis_mode: daemon\n---\n\nOriginal body.\n";
    std::fs::write(&page_path, original).expect("page written");
    let mut session = session_with_note(&scope, "Compile behavior", "raw/research/compile.md");
    let mut generated = false;
    let mut generator = |_prompt: &ExplainerPrompt| {
        generated = true;
        unreachable!("identity validation must run before generation")
    };

    let error = compile_to_wiki_with_options(
        &mut session,
        CompileRequest {
            topic: "Requested Topic".to_string(),
            outline: vec!["Overview".to_string()],
            target_page: Some(target),
            write_intent: true,
        },
        WikiCompileOptions::default(),
        Some(&mut generator),
    )
    .expect_err("mismatched target title must fail");

    match error {
        WikiError::InvalidInput { field, .. } => assert_eq!(field, "target_page"),
        other => panic!("unexpected error: {other:?}"),
    }
    assert!(!generated, "generator must remain untouched");
    assert_eq!(
        std::fs::read_to_string(&page_path).expect("page retained"),
        original
    );
}

#[test]
fn allow_target_identity_mismatch_permits_upkeep_merge_into_existing_page() {
    let temp = tempfile::tempdir().expect("tempdir");
    let scope = ResearchScope::project_for_id("project-1", temp.path());
    let note_path = scope.root().join("raw/research/compile.md");
    std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
    std::fs::write(&note_path, "Compile evidence.\n").expect("note written");
    let target = PathBuf::from("knowledge/topics/unrelated.md");
    let page_path = scope.root().join(&target);
    std::fs::create_dir_all(page_path.parent().expect("target parent")).expect("target dir");
    let original = "---\ntitle: Unrelated Topic\nsynthesis_mode: daemon\n---\n\nOriginal body.\n";
    std::fs::write(&page_path, original).expect("page written");
    let mut session = session_with_note(&scope, "Compile behavior", "raw/research/compile.md");

    let outcome = compile_to_wiki_with_options(
        &mut session,
        CompileRequest {
            topic: "Requested Topic".to_string(),
            outline: vec!["Overview".to_string()],
            target_page: Some(target.clone()),
            write_intent: true,
        },
        WikiCompileOptions {
            allow_target_identity_mismatch: true,
            ..WikiCompileOptions::default()
        },
        None,
    )
    .expect("upkeep-style merge compiles into the mismatched target");

    assert_eq!(outcome.article_path, scope.root().join(&target));
    let merged = std::fs::read_to_string(&page_path).expect("page rewritten");
    assert_ne!(merged, original, "merge must rewrite the target page");
    assert!(merged.contains("Requested Topic"), "{merged}");
}

#[test]
fn explicit_target_identity_is_rechecked_after_generation() {
    let temp = tempfile::tempdir().expect("tempdir");
    let scope = ResearchScope::project_for_id("project-1", temp.path());
    let note_path = scope.root().join("raw/research/compile.md");
    std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
    std::fs::write(&note_path, "Compile evidence.\n").expect("note written");
    let target = PathBuf::from("knowledge/topics/requested-topic.md");
    let page_path = scope.root().join(&target);
    std::fs::create_dir_all(page_path.parent().expect("target parent")).expect("target dir");
    std::fs::write(
        &page_path,
        "---\ntitle: Requested Topic\nsynthesis_mode: daemon\n---\n\nOriginal body.\n",
    )
    .expect("page written");
    let intruder = "---\ntitle: Different Topic\nsynthesis_mode: daemon\n---\n\nConcurrent body.\n";
    let mut session = session_with_note(&scope, "Compile behavior", "raw/research/compile.md");
    let mut generator = |_prompt: &ExplainerPrompt| {
        std::fs::write(&page_path, intruder).expect("concurrent page replacement");
        Ok(ExplainerResponse {
            text: "## Overview\nGenerated body [source: raw/research/compile.md].\n".to_string(),
            model: Some("mock-model".to_string()),
            route: "daemon",
            tool_use_count: None,
            turns: None,
            usage: None,
        })
    };

    let error = compile_to_wiki_with_options(
        &mut session,
        CompileRequest {
            topic: "Requested Topic".to_string(),
            outline: vec!["Overview".to_string()],
            target_page: Some(target),
            write_intent: true,
        },
        WikiCompileOptions::default(),
        Some(&mut generator),
    )
    .expect_err("identity replacement after generation must fail");

    match error {
        WikiError::InvalidInput { field, .. } => assert_eq!(field, "target_page"),
        other => panic!("unexpected error: {other:?}"),
    }
    assert_eq!(
        std::fs::read_to_string(&page_path).expect("replacement retained"),
        intruder
    );
}

#[test]
fn explicit_target_requires_parseable_non_empty_title() {
    for (name, existing) in [
        ("missing", "# Missing frontmatter title\n"),
        ("empty", "---\ntitle: '   '\n---\n\nEmpty title.\n"),
        ("malformed", "---\ntitle: Requested Topic\n"),
    ] {
        let temp = tempfile::tempdir().expect("tempdir");
        let scope = ResearchScope::project_for_id("project-1", temp.path());
        let note_path = scope.root().join("raw/research/compile.md");
        std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
        std::fs::write(&note_path, "Compile evidence.\n").expect("note written");
        let target = PathBuf::from(format!("knowledge/topics/{name}.md"));
        let page_path = scope.root().join(&target);
        std::fs::create_dir_all(page_path.parent().expect("target parent")).expect("target dir");
        std::fs::write(&page_path, existing).expect("page written");
        let mut session = session_with_note(&scope, "Compile behavior", "raw/research/compile.md");

        let error = compile_to_wiki(
            &mut session,
            CompileRequest {
                topic: "Requested Topic".to_string(),
                outline: Vec::new(),
                target_page: Some(target),
                write_intent: true,
            },
        )
        .expect_err("invalid target title must fail closed");

        match error {
            WikiError::InvalidInput { field, .. } => assert_eq!(field, "target_page", "{name}"),
            other => panic!("unexpected {name} error: {other:?}"),
        }
        assert_eq!(
            std::fs::read_to_string(&page_path).expect("page retained"),
            existing,
            "{name} target changed"
        );
    }
}

#[test]
fn recompile_over_hand_authored_page_requires_write_intent() {
    let temp = tempfile::tempdir().expect("tempdir");
    let scope = ResearchScope::project_for_id("project-1", temp.path());
    let note_path = scope.root().join("raw/research/compile.md");
    std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
    std::fs::write(&note_path, "First compile evidence.\n").expect("note written");
    let target = PathBuf::from("knowledge/topics/hand-authored.md");
    let page_path = scope.root().join(&target);
    std::fs::create_dir_all(page_path.parent().expect("target parent")).expect("target dir");
    let curated = "---\ntitle: Hand Authored\n---\n\n# Hand authored\n\nCurated by a human.\n";
    std::fs::write(&page_path, curated).expect("page written");

    // A page without `synthesis_mode` provenance is not machine-owned, so the
    // #17635 anti-clobber guard still fails an intent-less recompile loud and
    // never clobbers the human page.
    let mut session = session_with_note(&scope, "Compile behavior", "raw/research/compile.md");
    let error = compile_to_wiki(
        &mut session,
        CompileRequest {
            topic: "Hand Authored".to_string(),
            outline: vec!["Overview".to_string()],
            target_page: Some(target.clone()),
            write_intent: false,
        },
    )
    .expect_err("recompile over a hand-authored page fails loud without write intent");
    assert_eq!(error.code(), "invalid_input");
    assert_eq!(
        std::fs::read_to_string(&page_path).expect("page retained"),
        curated,
        "the human page is never clobbered"
    );
}

#[test]
fn recompile_carries_existing_target_body_into_prompt() {
    let temp = tempfile::tempdir().expect("tempdir");
    let scope = ResearchScope::project_for_id("project-1", temp.path());
    let note_path = scope.root().join("raw/research/compile.md");
    std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
    std::fs::write(&note_path, "Compile evidence.\n").expect("note written");
    let target_path = scope.root().join("knowledge/topics/durable-compile.md");
    std::fs::create_dir_all(target_path.parent().expect("target parent")).expect("topics dir");
    std::fs::write(
        &target_path,
        "---\ntitle: Durable Compile\n---\n\n## Overview\n\nPreviously compiled claim.\n",
    )
    .expect("target written");
    let mut session = session_with_note(&scope, "Compile behavior", "raw/research/compile.md");

    let outcome = compile_to_wiki_with_options(
        &mut session,
        CompileRequest {
            topic: "Durable Compile".to_string(),
            outline: vec!["Overview".to_string()],
            target_page: Some(PathBuf::from("knowledge/topics/durable-compile.md")),
            write_intent: true,
        },
        WikiCompileOptions::default(),
        None,
    )
    .expect("recompile succeeded");

    assert!(
        outcome.prompt.user.contains("Current page content"),
        "{}",
        outcome.prompt.user
    );
    assert!(outcome.prompt.user.contains("Previously compiled claim."));
    // Frontmatter is stripped before the body enters the prompt.
    assert!(!outcome.prompt.user.contains("title: Durable Compile"));

    let handoff = std::fs::read_to_string(
        scope
            .root()
            .join("_gwiki/compile")
            .join(format!("{}.md", outcome.handoff_id)),
    )
    .expect("persisted handoff");
    assert!(
        handoff.contains("# Compile bundle: Durable Compile"),
        "{handoff}"
    );
    assert!(
        handoff.contains("## Target page\n\n- knowledge/topics/durable-compile.md"),
        "{handoff}"
    );
    assert!(handoff.contains("## Write intent\n\n- true"), "{handoff}");
    assert!(handoff.contains("raw/research/compile.md"), "{handoff}");
}

#[test]
fn compile_regenerates_index_catalog_from_vault_state() {
    let temp = tempfile::tempdir().expect("tempdir");
    let scope = ResearchScope::project_for_id("project-1", temp.path());
    let topics_dir = scope.root().join("knowledge/topics");
    std::fs::create_dir_all(&topics_dir).expect("topics dir");
    std::fs::write(
        topics_dir.join("existing.md"),
        "---\ntitle: \"Existing Entry\"\n---\n\nAlready compiled body.\n",
    )
    .expect("existing page written");
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
    assert!(index.contains("## Overview"), "{index}");
    assert!(
        index.contains("[[knowledge/topics/existing|Existing Entry]]"),
        "{index}"
    );
    assert!(
        index.contains("[[knowledge/topics/index-preservation|Index Preservation]]"),
        "{index}"
    );
    let knowledge = std::fs::read_to_string(scope.root().join("knowledge/INDEX.md"))
        .expect("knowledge index read");
    assert!(
        knowledge.contains("[[knowledge/topics/index-preservation|Index Preservation]]"),
        "{knowledge}"
    );
}

#[test]
fn compile_without_checkpoint_persistence_leaves_research_session_untouched() {
    let temp = tempfile::tempdir().expect("tempdir");
    let scope = ResearchScope::project_for_id("project-1", temp.path());
    let note_path = scope.root().join("raw/research/ephemeral.md");
    std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
    std::fs::write(&note_path, "Ephemeral compile evidence.").expect("note written");
    let mut session = session_with_note(&scope, "Ephemeral", "raw/research/ephemeral.md");

    compile_to_wiki_with_options(
        &mut session,
        CompileRequest {
            topic: "Ephemeral Compile".to_string(),
            outline: vec!["Overview".to_string()],
            target_page: None,
            write_intent: false,
        },
        WikiCompileOptions {
            persist_checkpoint: false,
            ..WikiCompileOptions::default()
        },
        None,
    )
    .expect("wiki article compiled");

    // Compile state is recorded in memory for the caller...
    assert!(session.compile_state.is_some());
    // ...but the on-disk research checkpoint is never written.
    let checkpoint = ResearchSession::checkpoint_path(scope.root());
    assert!(
        !checkpoint.exists(),
        "persist_checkpoint=false must not write {}",
        checkpoint.display()
    );
}

#[test]
fn compile_appends_page_write_log_entries() {
    let temp = tempfile::tempdir().expect("tempdir");
    let scope = ResearchScope::project_for_id("project-1", temp.path());
    let note_path = scope.root().join("raw/research/logged.md");
    std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
    std::fs::write(&note_path, "Compile writes go to the log.").expect("note written");
    let mut session = session_with_note(&scope, "Logged compile", "raw/research/logged.md");

    compile_to_wiki(
        &mut session,
        CompileRequest {
            topic: "Logged Compile".to_string(),
            outline: vec!["Overview".to_string()],
            target_page: None,
            write_intent: false,
        },
    )
    .expect("wiki article compiled");

    let log = std::fs::read_to_string(scope.root().join("log.md")).expect("log read");
    let article_line = log
        .lines()
        .find(|line| line.contains("knowledge/topics/logged-compile.md"))
        .expect("article write logged");
    assert!(article_line.starts_with("- "), "{article_line}");
    assert!(article_line.contains("page_created:"), "{article_line}");
    assert!(article_line.contains("Logged Compile"), "{article_line}");
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
                tool_use_count: Some(4),
                turns: Some(3),
                usage: Some(TokenUsage {
                    input_tokens: Some(120),
                    output_tokens: Some(45),
                    total_tokens: Some(165),
                }),
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
    assert_eq!(report.tool_use_count, Some(4));
    assert_eq!(report.turns, Some(3));
    assert_eq!(
        report.usage.as_ref().and_then(TokenUsage::token_count),
        Some(165)
    );
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
fn compile_tool_loop_failure_hard_fails_without_skeleton() {
    let temp = tempfile::tempdir().expect("tempdir");
    let scope = ResearchScope::project_for_id("project-1", temp.path());
    let note_path = scope.root().join("raw/research/compile.md");
    std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
    std::fs::write(&note_path, "Accepted compile evidence.").expect("note written");
    let mut session = session_with_note(&scope, "Compile behavior", "raw/research/compile.md");

    let mut generator = |_prompt: &ExplainerPrompt| {
        Err::<ExplainerResponse, _>("tool loop unavailable".to_string())
    };
    // With tool-loop hard-fail set, a generation failure must NOT write a skeleton
    // article — it hard-fails with a distinct error (#982, matching codewiki #978).
    let error = compile_to_wiki_with_options(
        &mut session,
        CompileRequest {
            topic: "Hard Fail Compile".to_string(),
            outline: vec!["Overview".to_string()],
            target_page: Some(PathBuf::from("hard-fail-compile.md")),
            write_intent: true,
        },
        WikiCompileOptions {
            hard_fail_on_generation_failure: true,
            ..WikiCompileOptions::default()
        },
        Some(&mut generator),
    )
    .expect_err("tool-loop failure hard-fails");

    assert!(
        matches!(error, crate::WikiError::Generation { .. }),
        "expected a generation error, got: {error}"
    );
    let message = error.to_string();
    assert!(
        message.contains("Tool-loop compile generation failed"),
        "{message}"
    );
    assert!(message.contains("no skeleton"), "{message}");

    // No synthesized article was written under the vault's knowledge tree.
    assert!(
        !scope.root().join("knowledge").exists(),
        "no skeleton article should be written on tool-loop hard-fail"
    );
    assert!(
        !scope.root().join("hard-fail-compile.md").exists(),
        "write-intent target handoff should not be written on tool-loop hard-fail"
    );
}

#[test]
fn compile_drops_alias_equal_to_title_and_keeps_case_variants() {
    // Observed case variants become frontmatter aliases, but the variant
    // identical to the page title is pure redundancy — the title is already a
    // resolution key — and every upkeep pass would rewrite it (#17642).
    let temp = tempfile::tempdir().expect("tempdir");
    let scope = ResearchScope::project_for_id("project-1", temp.path());
    let note_path = scope.root().join("raw/research/compile.md");
    std::fs::create_dir_all(note_path.parent().expect("note parent")).expect("raw dir");
    std::fs::write(&note_path, "Accepted compile evidence.").expect("note written");
    let mut session = session_with_note(&scope, "Compile behavior", "raw/research/compile.md");

    let outcome = compile_to_wiki_with_options(
        &mut session,
        CompileRequest {
            topic: "Gcode".to_string(),
            outline: vec!["Overview".to_string()],
            target_page: None,
            write_intent: false,
        },
        WikiCompileOptions {
            target_kind: ArticleKind::Concept,
            aliases: vec!["Gcode".to_string(), "gcode".to_string()],
            ..WikiCompileOptions::default()
        },
        None,
    )
    .expect("wiki article compiled");

    let page = std::fs::read_to_string(&outcome.article_path).expect("article written");
    let parsed = crate::frontmatter::parse_frontmatter(&page).expect("frontmatter parses");
    assert_eq!(parsed.metadata.title.as_deref(), Some("Gcode"));
    assert_eq!(parsed.metadata.aliases, vec!["gcode"]);
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
