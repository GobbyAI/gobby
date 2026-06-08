use super::*;
use crate::research::notes::{
    accepted_note_draft_id, frontmatter_block, render_accepted_note_body, write_accepted_note,
    yaml_field_eq,
};
use crate::scope;

fn default_options(question: &str, scope: ResearchScope) -> ResearchOptions {
    ResearchOptions {
        question: question.to_string(),
        scope,
        source_constraints: Vec::new(),
        audit: false,
        max_steps: 12,
        max_tokens: 24_000,
        max_sources: 8,
        ai: AiRouting::Direct,
        require_ai: false,
        accepted_notes: Vec::new(),
    }
}

fn write_test_source(root: &Path) {
    let raw_dir = root.join("raw");
    std::fs::create_dir_all(&raw_dir).expect("raw dir");
    std::fs::write(raw_dir.join("source.md"), "source").expect("source file");
}

#[test]
fn frontmatter_block_accepts_crlf_delimiters() {
    let markdown = "---\r\nresearch_note_id: abc\r\nresearch_status: completed\r\n---\r\nBody";
    let frontmatter = frontmatter_block(markdown).expect("frontmatter");

    assert!(yaml_field_eq(frontmatter, "research_note_id", "abc"));
    assert!(yaml_field_eq(frontmatter, "research_status", "completed"));
}

#[test]
fn yaml_field_eq_requires_exact_key_and_value() {
    let frontmatter = r#"
research_note_id_suffix: abc
research_note_id: abc-extra
research_status: "completed"
research_mode: completed now
"#;

    assert!(!yaml_field_eq(frontmatter, "research_note_id", "abc"));
    assert!(yaml_field_eq(frontmatter, "research_status", "completed"));
    assert!(!yaml_field_eq(frontmatter, "research_mode", "completed"));
}

#[test]
fn yaml_field_eq_rejects_block_scalar_prefix_match() {
    let frontmatter = r#"
research_note_id: |
  abc
research_status: completed
"#;

    assert!(!yaml_field_eq(frontmatter, "research_note_id", "abc"));
    assert!(yaml_field_eq(frontmatter, "research_status", "completed"));
}

#[test]
fn research_reloads_checkpoint_without_daemon_dispatch() {
    let temp = tempfile::tempdir().expect("tempdir");
    let scope = ResearchScope::project_for_id("project-1", temp.path());
    let checkpoint = ResearchSession {
        session_id: "research-existing".to_string(),
        question: "What changed in the parser?".to_string(),
        prompt: "Research parser changes".to_string(),
        scope: scope.clone(),
        source_constraints: vec!["official docs only".to_string()],
        agent_count: 2,
        dispatch_task_id: Some("#300".to_string()),
        dispatch: Some(crate::session::DaemonDispatch {
            dispatch_id: "dispatch-existing".to_string(),
            daemon_base_url: "http://daemon.test".to_string(),
            agent_run_ids: vec!["run-a".to_string(), "run-b".to_string()],
        }),
        accepted_notes: Vec::new(),
        compile_state: None,
    };
    checkpoint.save_checkpoint().expect("checkpoint saved");

    let outcome = run(default_options("What should be refreshed?", scope))
        .expect("research checkpoint loaded");

    assert_eq!(outcome.session.session_id, "research-existing");
    assert_eq!(outcome.session.agent_count, 1);
    assert_eq!(outcome.session.dispatch_task_id, None);
    assert_eq!(outcome.session.dispatch, None);
    assert_eq!(outcome.session.question, "What should be refreshed?");
}

#[test]
fn enrichment_require_ai_rejects_ai_off() {
    let temp = tempfile::tempdir().expect("tempdir");
    let mut options = default_options(
        "What should be researched?",
        ResearchScope::project_for_id("project-1", temp.path()),
    );
    options.ai = AiRouting::Off;
    options.require_ai = true;
    let error = run(options).expect_err("required AI off should be rejected for enrichment");

    assert!(matches!(error, WikiError::Config { .. }));
}

#[test]
fn accepted_notes_land_in_raw_research() {
    let temp = tempfile::tempdir().expect("tempdir");
    let scope = ResearchScope::project_for_id("project-1", temp.path());
    write_test_source(scope.root());

    let mut options = default_options("How should events be monitored?", scope.clone());
    options.source_constraints = vec!["source-linked notes".to_string()];
    options.max_steps = 0;
    options.accepted_notes = vec![AcceptedNoteDraft {
        title: "Session event monitoring".to_string(),
        body: "Durable event logs are appended as JSONL.".to_string(),
        sources: vec!["raw/source.md".to_string()],
        code_citations: Vec::new(),
        degradation: None,
    }];
    let outcome = run(options).expect("research ran");

    let note = outcome
        .session
        .accepted_notes
        .first()
        .expect("accepted note recorded");
    assert_eq!(outcome.session.dispatch_task_id, None);
    assert_eq!(outcome.max_steps, 0);
    assert_eq!(outcome.max_tokens, 24_000);
    assert_eq!(outcome.max_sources, 8);
    assert!(!outcome.write_conflict);
    assert!(note.path.starts_with(scope.root().join("raw/research")));
    assert_eq!(
        note.path.file_name().and_then(|name| name.to_str()),
        Some("session-event-monitoring.md")
    );

    let note_text = std::fs::read_to_string(&note.path).expect("note written");
    assert!(note_text.contains("title: Session event monitoring"));
    assert!(note_text.contains("research_status: completed"));
    assert!(note_text.contains("research_note_id:"));
    assert!(note_text.contains("sources:"));
    assert!(note_text.contains("raw/source.md"));
    assert!(note_text.contains("Durable event logs are appended as JSONL."));

    let index = std::fs::read_to_string(scope.root().join("raw/INDEX.md"))
        .expect("raw index includes note");
    assert!(index.contains("raw/research/session-event-monitoring.md"));
}

#[test]
fn accepted_note_collisions_use_numeric_suffixes() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    let first = write_accepted_note(
        root,
        "research-1",
        &AcceptedNoteDraft {
            title: "Same title".to_string(),
            body: "first".to_string(),
            sources: Vec::new(),
            code_citations: Vec::new(),
            degradation: None,
        },
    )
    .expect("first note");
    let second = write_accepted_note(
        root,
        "research-1",
        &AcceptedNoteDraft {
            title: "Same title".to_string(),
            body: "second".to_string(),
            sources: Vec::new(),
            code_citations: Vec::new(),
            degradation: None,
        },
    )
    .expect("second note");

    assert_eq!(
        first.note.path.file_name().and_then(|name| name.to_str()),
        Some("same-title.md")
    );
    assert_eq!(
        second.note.path.file_name().and_then(|name| name.to_str()),
        Some("same-title-2.md")
    );
    // A different note sharing the title slug is a legitimate numeric-suffix
    // collision, not a write conflict.
    assert!(!first.write_conflict);
    assert!(!second.write_conflict);
}

#[test]
fn accepted_note_draft_collision_with_changed_body_is_write_conflict() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    let research_dir = root.join("raw/research");
    std::fs::create_dir_all(&research_dir).expect("research dir");

    let draft = AcceptedNoteDraft {
        title: "Concurrent note".to_string(),
        body: "the validated draft body".to_string(),
        sources: Vec::new(),
        code_citations: Vec::new(),
        degradation: None,
    };
    let draft_id = accepted_note_draft_id(&draft);
    // Simulate a concurrent writer: a completed note carrying our draft id
    // but a body that changed since draft validation.
    let tampered = AcceptedNoteDraft {
        title: draft.title.clone(),
        body: "a different body written by another process".to_string(),
        sources: draft.sources.clone(),
        code_citations: draft.code_citations.clone(),
        degradation: draft.degradation.clone(),
    };
    let path = research_dir.join("concurrent-note.md");
    let on_disk =
        render_accepted_note_body("research-other", &tampered, &draft_id, "completed", true)
            .expect("tampered note body");
    std::fs::write(&path, &on_disk).expect("write tampered note");

    let result = write_accepted_note(root, "research-1", &draft).expect("write result");

    assert!(result.write_conflict);
    assert!(!result.created);
    assert_eq!(result.note.path, path);
    // The existing note is not overwritten and no suffix-bumped note is made.
    assert_eq!(
        std::fs::read_to_string(&path).expect("note still present"),
        on_disk
    );
    assert!(!research_dir.join("concurrent-note-2.md").exists());
}

#[test]
fn accepted_notes_are_idempotent_by_draft_id() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    let draft = AcceptedNoteDraft {
        title: "Same title".to_string(),
        body: "same body".to_string(),
        sources: vec!["same-source.md".to_string()],
        code_citations: Vec::new(),
        degradation: None,
    };

    let first = write_accepted_note(root, "research-1", &draft).expect("first note");
    let second = write_accepted_note(root, "research-1", &draft).expect("second note");

    assert!(first.created);
    assert!(!second.created);
    assert!(!first.write_conflict);
    assert!(!second.write_conflict);
    assert_eq!(first.note.path, second.note.path);
    let index = std::fs::read_to_string(root.join("raw/INDEX.md")).expect("raw index");
    assert_eq!(index.matches("raw/research/same-title.md").count(), 1);
    assert!(!root.join("raw/research/same-title-2.md").exists());
}

#[test]
fn accepted_note_waits_for_materializing_marker_to_complete() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    let research_dir = root.join("raw/research");
    std::fs::create_dir_all(&research_dir).expect("research dir");
    let draft = AcceptedNoteDraft {
        title: "Shared note".to_string(),
        body: "same body".to_string(),
        sources: Vec::new(),
        code_citations: Vec::new(),
        degradation: None,
    };
    let draft_id = accepted_note_draft_id(&draft);
    let path = research_dir.join("shared-note.md");
    let marker = render_accepted_note_body("research-1", &draft, &draft_id, "materializing", false)
        .expect("marker");
    std::fs::write(&path, marker).expect("write materializing marker");
    let completed_path = path.clone();
    let completed_draft = draft.clone();
    let completed_id = draft_id.clone();
    let (wait_observed_tx, wait_observed_rx) = std::sync::mpsc::channel();
    let _wait_signal_guard = set_materializing_wait_signal(wait_observed_tx);
    let completer = std::thread::spawn(move || {
        wait_observed_rx.recv().expect("wait observed");
        let body = render_accepted_note_body(
            "research-1",
            &completed_draft,
            &completed_id,
            "completed",
            true,
        )
        .expect("completed body");
        std::fs::write(completed_path, body).expect("complete marker");
    });

    let accepted = write_accepted_note(root, "research-1", &draft).expect("accepted note");
    completer.join().expect("completed marker writer joined");

    assert!(!accepted.created);
    assert_eq!(accepted.note.path, path);
}

#[test]
fn research_scope_from_resolved_carries_project_id() {
    let temp = tempfile::tempdir().expect("tempdir");
    let resolved = scope::ResolvedScope::project(
        "project-123".to_string(),
        temp.path().to_path_buf(),
        temp.path().join(".gwiki"),
    );

    let scope = research_scope_from_resolved(&resolved);

    match scope {
        ResearchScope::Project { project_id, root } => {
            assert_eq!(project_id, "project-123");
            assert_eq!(root, temp.path().join(".gwiki"));
        }
        ResearchScope::Topic { .. } => panic!("expected project scope"),
    }
}

#[test]
fn deterministic_audit_reports_untracked_completed_note() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    let research_dir = root.join("raw/research");
    std::fs::create_dir_all(&research_dir).expect("research dir");
    let note_path = research_dir.join("orphan.md");
    std::fs::write(
        &note_path,
        "---\nresearch_note_id: orphan\nresearch_status: completed\n---\n\nBody\n",
    )
    .expect("orphan note");

    let mut options = default_options(
        "Audit wiki scope",
        ResearchScope::project_for_id("project-1", root),
    );
    options.audit = true;
    options.ai = AiRouting::Off;
    let outcome = run(options).expect("audit ran");

    assert!(outcome.audit);
    assert_eq!(outcome.status, ResearchStatus::Partial);
    assert_eq!(outcome.stop_reason, ResearchStopReason::AiUnavailable);
    assert_eq!(outcome.findings.len(), 1);
    assert_eq!(outcome.findings[0].evidence, vec![note_path]);
    assert_eq!(
        outcome.findings[0].kind,
        "orphaned_raw_research_note".to_string()
    );
    assert_eq!(
        outcome.warnings,
        vec!["ai_unavailable: model-assisted audit checks skipped".to_string()]
    );
}

#[test]
fn deterministic_audit_uses_checkpoint_inventory() {
    let temp = tempfile::tempdir().expect("tempdir");
    let scope = ResearchScope::project_for_id("project-1", temp.path());
    write_test_source(scope.root());
    let mut options = default_options("Record accepted note", scope.clone());
    options.max_steps = 0;
    options.accepted_notes = vec![AcceptedNoteDraft {
        title: "Tracked note".to_string(),
        body: "Tracked body.".to_string(),
        sources: vec!["raw/source.md".to_string()],
        code_citations: Vec::new(),
        degradation: None,
    }];
    let setup = run(options).expect("accepted note written");
    assert_eq!(setup.session.accepted_notes.len(), 1);

    let mut audit_options = default_options("Audit wiki scope", scope);
    audit_options.audit = true;
    audit_options.ai = AiRouting::Off;
    let outcome = run(audit_options).expect("audit ran");

    assert!(outcome.findings.is_empty());
}
