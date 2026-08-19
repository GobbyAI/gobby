use super::*;
use gobby_wiki::session::DaemonDispatch;

#[test]
fn public_cli_smoke_uses_gwiki_modules() {
    let fixture = common::GwikiFixture::new();
    let source = fixture.root().join("ownership-source.md");
    fs::write(
        &source,
        "# Ownership Source\n\nOwnership evidence for Rust borrowing.\n",
    )
    .expect("write source");

    let init = gwiki(
        &fixture,
        fixture.root(),
        &["--format", "json", "init", "--topic", "rust"],
    );
    common::assert_success(&init, "init");

    let vault = fixture.topic_vault("rust");
    fs::create_dir_all(vault.join("knowledge/topics")).expect("create topic dir");
    fs::write(
        vault.join("knowledge/topics/ownership.md"),
        "# Ownership\n\nOwnership explains borrowing.\n",
    )
    .expect("write ownership page");
    fs::write(
        vault.join("knowledge/topics/rust.md"),
        "# Rust\n\nRust links to [[Ownership]]. Missing [[Borrow checker]].\n",
    )
    .expect("write rust page");

    let ingest = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format",
            "json",
            "ingest-file",
            "--topic",
            "rust",
            source.to_str().expect("source path utf8"),
        ],
    );
    assert_daemon_required(&ingest, "ingest-file");

    let index = gwiki(
        &fixture,
        fixture.root(),
        &["--format", "json", "index", "--topic", "rust"],
    );
    assert_daemon_required(&index, "index");

    let search = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format",
            "json",
            "search",
            "--topic",
            "rust",
            "ownership",
            "--limit",
            "3",
        ],
    );
    assert_daemon_required(&search, "search");

    let backlinks = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format",
            "json",
            "backlinks",
            "--topic",
            "rust",
            "knowledge/topics/ownership.md",
        ],
    );
    assert_daemon_required(&backlinks, "backlinks");

    let suggestions = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format",
            "json",
            "link-suggest",
            "--topic",
            "rust",
            "--limit",
            "3",
        ],
    );
    assert_daemon_required(&suggestions, "link-suggest");

    seed_accepted_research_checkpoint(&vault);

    let compile = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format",
            "json",
            "--topic",
            "rust",
            "compile",
            "--no-ai",
            "--outline",
            "Overview",
            "--target",
            "knowledge/topics/ownership-synthesis.md",
        ],
    );
    common::assert_success(&compile, "compile");
    let compile_payload = common::json_stdout(&compile);
    assert_eq!(compile_payload["command"], "compile");
    assert_json_path(
        &compile_payload["article_path"],
        &vault.join("knowledge/topics/ownership-synthesis.md"),
    );
    assert!(
        vault
            .join("knowledge/sources/ownership-evidence.md")
            .is_file()
    );

    let audit = gwiki(
        &fixture,
        fixture.root(),
        &["--format", "json", "--topic", "rust", "audit"],
    );
    common::assert_success(&audit, "audit");
    let audit_payload = common::json_stdout(&audit);
    assert_eq!(audit_payload["command"], "audit");
    assert_json_path(&audit_payload["root"], &vault);
}

#[test]
fn public_cli_smoke_compiles_accepted_notes_and_audits_in_topic_scope() {
    let fixture = common::GwikiFixture::new();

    let init = gwiki(
        &fixture,
        fixture.root(),
        &["--format", "json", "init", "--topic", "rust"],
    );
    common::assert_success(&init, "init");

    let vault = fixture.topic_vault("rust");
    let note_path = vault.join("raw/research/session-scope.md");
    fs::create_dir_all(note_path.parent().expect("note parent")).expect("create research dir");
    fs::write(
        &note_path,
        "---\ntitle: Session scope\nindexable: true\n---\n\ncitation: Gobby wiki scope resolver\nTopic research should use the configured hub.\n",
    )
    .expect("write research note");

    let mut session = ResearchSession::new(
        "How should gwiki resolve topic research scope?",
        ResearchScope::topic("rust", &vault),
        vec!["Use local smoke fixture".to_string()],
        1,
        Some("#300".to_string()),
    )
    .expect("research session");
    session.dispatch = Some(DaemonDispatch {
        dispatch_id: "dispatch-smoke".to_string(),
        daemon_base_url: "http://daemon.test".to_string(),
        agent_run_ids: vec!["run-smoke".to_string()],
    });
    session.accepted_notes.push(AcceptedResearchNote {
        title: "Session scope".to_string(),
        path: note_path,
        code_citations: Vec::new(),
        degradation: None,
    });
    session.save_checkpoint().expect("save checkpoint");

    let compile = gwiki(
        &fixture,
        fixture.root(),
        &["--format", "json", "--topic", "rust", "compile", "--no-ai"],
    );
    common::assert_success(&compile, "compile");
    let compile_payload = common::json_stdout(&compile);
    assert_eq!(compile_payload["command"], "compile");
    assert!(
        compile_payload["article_path"]
            .as_str()
            .is_some_and(|path| path.ends_with("knowledge/topics/rust.md")),
        "{compile_payload:#}"
    );

    let audit = gwiki(
        &fixture,
        fixture.root(),
        &["--format", "json", "--topic", "rust", "audit"],
    );
    common::assert_success(&audit, "audit");
    let audit_payload = common::json_stdout(&audit);
    assert_eq!(audit_payload["command"], "audit");
    assert_eq!(audit_payload["scope"]["kind"], "topic");
    assert_eq!(audit_payload["scope"]["id"], "rust");
}

#[test]
fn public_cli_smoke_compile_source_ingest_requires_daemon() {
    let fixture = common::GwikiFixture::new();
    let topic = fixture.init_topic("fresh-compile");
    let source = fixture.project().join("fresh-source.md");
    fs::write(
        &source,
        "# Fresh source\n\nFresh topic compile should select this ingested note.\n",
    )
    .expect("write source");
    let ingest = gwiki(
        &fixture,
        fixture.project(),
        &[
            "--format",
            "json",
            "--topic",
            &topic.name,
            "ingest-file",
            "--no-ai",
            source.to_str().expect("source path utf8"),
        ],
    );
    assert!(
        !ingest.status.success(),
        "ingest-file succeeded without a daemon"
    );
    assert_daemon_required(&ingest, "ingest-file");
}

#[test]
fn public_cli_smoke_targeted_project_compile_requires_topic_before_writes() {
    let fixture = common::GwikiFixture::new();
    common::write_gcode_json(fixture.project());
    let init = gwiki(
        &fixture,
        fixture.project(),
        &["--format", "json", "init", "--project"],
    );
    common::assert_success(&init, "project init");

    let vault = fixture.project().join("wiki");
    let target = vault.join("knowledge/topics/ambiguous.md");
    let handoff_dir = vault.join("_gwiki/compile");
    let handoff_count = || {
        std::fs::read_dir(&handoff_dir)
            .map(|entries| entries.filter_map(Result::ok).count())
            .unwrap_or(0)
    };
    let before_handoffs = handoff_count();

    let compile = gwiki(
        &fixture,
        fixture.project(),
        &[
            "--format",
            "json",
            "--project",
            "compile",
            "--target",
            "knowledge/topics/ambiguous.md",
            "--write-intent",
            "--no-ai",
        ],
    );

    assert!(!compile.status.success(), "ambiguous compile succeeded");
    let error = common::json_stderr(&compile);
    assert_eq!(error["code"], "invalid_input");
    assert!(
        error["message"]
            .as_str()
            .is_some_and(|message| message.contains("topic") && message.contains("--target")),
        "{error:#}"
    );
    assert!(!target.exists(), "ambiguous target was written");
    assert!(
        !vault.join("_gwiki/research-session.json").exists(),
        "ambiguous compile wrote a checkpoint"
    );
    assert_eq!(handoff_count(), before_handoffs, "compile wrote a handoff");
}
