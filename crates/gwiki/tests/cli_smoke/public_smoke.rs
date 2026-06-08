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

    let setup = gwiki(
        &fixture,
        fixture.root(),
        &["--format", "json", "setup", "--topic", "rust"],
    );
    common::assert_success(&setup, "setup");
    let setup_payload = common::json_stdout(&setup);
    assert!(
        setup_payload["objects"]
            .as_array()
            .is_some_and(|objects| !objects.is_empty()),
        "{setup_payload:#}"
    );

    let vault = fixture.topic_vault("rust");
    fs::create_dir_all(vault.join("wiki/topics")).expect("create topic dir");
    fs::write(
        vault.join("wiki/topics/ownership.md"),
        "# Ownership\n\nOwnership explains borrowing.\n",
    )
    .expect("write ownership page");
    fs::write(
        vault.join("wiki/topics/rust.md"),
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
    common::assert_success(&ingest, "ingest-file");
    let ingest_payload = common::json_stdout(&ingest);
    assert_eq!(ingest_payload["command"], "ingest-file");
    assert!(ingest_payload["raw_path"].as_str().is_some());

    let index = gwiki(
        &fixture,
        fixture.root(),
        &["--format", "json", "index", "--topic", "rust"],
    );
    common::assert_success(&index, "index");
    let index_payload = common::json_stdout(&index);
    assert!(
        index_payload["indexed"]["documents"]
            .as_u64()
            .is_some_and(|count| count >= 2),
        "{index_payload:#}"
    );
    assert!(
        index_payload["indexed"]["chunks"]
            .as_u64()
            .is_some_and(|count| count >= 2),
        "{index_payload:#}"
    );

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
    common::assert_success(&search, "search");
    let search_payload = common::json_stdout(&search);
    assert!(
        search_payload["results"]
            .as_array()
            .is_some_and(|results| !results.is_empty()),
        "{search_payload:#}"
    );
    let first_result = &search_payload["results"][0];
    assert!(
        first_result["sources"]
            .as_array()
            .is_some_and(|sources| sources.iter().any(|source| source == "bm25")),
        "{search_payload:#}"
    );
    assert!(
        first_result["explanations"]
            .as_array()
            .is_some_and(|explanations| !explanations.is_empty()),
        "{search_payload:#}"
    );

    let backlinks = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format",
            "json",
            "backlinks",
            "--topic",
            "rust",
            "wiki/topics/ownership.md",
        ],
    );
    common::assert_success(&backlinks, "backlinks");
    let backlinks_payload = common::json_stdout(&backlinks);
    assert!(
        backlinks_payload["backlinks"]
            .as_array()
            .is_some_and(|links| !links.is_empty()),
        "{backlinks_payload:#}"
    );

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
    common::assert_success(&suggestions, "link-suggest");
    let suggestions_payload = common::json_stdout(&suggestions);
    assert!(
        suggestions_payload["suggestions"]
            .as_array()
            .is_some_and(|suggestions| !suggestions.is_empty()),
        "{suggestions_payload:#}"
    );

    seed_accepted_research_checkpoint(&vault);

    let research = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format", "json", "--topic", "rust", "research", "--audit", "--ai", "off",
        ],
    );
    common::assert_success(&research, "research");
    let research_payload = common::json_stdout(&research);
    assert_eq!(research_payload["command"], "research");
    assert_eq!(research_payload["audit"], true);
    // `--ai off` exercises the deterministic audit path, so the loop should
    // stop because model-backed research is intentionally unavailable.
    assert_eq!(research_payload["stop_reason"], "ai_unavailable");
    assert_eq!(research_payload["scope"]["kind"], "topic");
    assert_eq!(research_payload["scope"]["id"], "rust");

    let compile = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format",
            "json",
            "--topic",
            "rust",
            "compile",
            "--outline",
            "Overview",
            "--target",
            "wiki/topics/ownership-synthesis.md",
        ],
    );
    common::assert_success(&compile, "compile");
    let compile_payload = common::json_stdout(&compile);
    assert_eq!(compile_payload["command"], "compile");
    assert_json_path(
        &compile_payload["article_path"],
        &vault.join("wiki/topics/ownership-synthesis.md"),
    );
    assert!(vault.join("wiki/sources/ownership-evidence.md").is_file());

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
fn public_cli_smoke_continues_research_compile_audit_in_topic_scope() {
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

    let research = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format", "json", "--topic", "rust", "research", "--audit", "--ai", "off",
        ],
    );
    common::assert_success(&research, "research");
    let research_payload = common::json_stdout(&research);
    assert_eq!(research_payload["command"], "research");
    assert_eq!(research_payload["scope"]["kind"], "topic");
    assert_eq!(research_payload["scope"]["id"], "rust");
    assert_eq!(research_payload["audit"], true);
    assert_eq!(research_payload["stop_reason"], "ai_unavailable");
    assert!(vault.join(".gwiki/session-events.jsonl").exists());

    let compile = gwiki(
        &fixture,
        fixture.root(),
        &["--format", "json", "--topic", "rust", "compile"],
    );
    common::assert_success(&compile, "compile");
    let compile_payload = common::json_stdout(&compile);
    assert_eq!(compile_payload["command"], "compile");
    assert!(
        compile_payload["article_path"]
            .as_str()
            .is_some_and(|path| path.ends_with("wiki/topics/rust.md")),
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
