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
    let results = search_payload["results"]
        .as_array()
        .expect("search results array");
    assert!(!results.is_empty(), "{search_payload:#}");
    assert!(
        results.iter().any(|result| result["sources"]
            .as_array()
            .is_some_and(|sources| sources.iter().any(|source| source == "bm25"))),
        "{search_payload:#}"
    );
    let first_result = &results[0];
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
            "knowledge/topics/ownership.md",
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

    let compile = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format",
            "json",
            "--topic",
            "rust",
            "compile",
            "--ai",
            "off",
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
        &[
            "--format", "json", "--topic", "rust", "compile", "--ai", "off",
        ],
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
fn public_cli_smoke_compile_source_bootstraps_fresh_project_checkpoint() {
    let fixture = common::GwikiFixture::new();
    common::write_gcode_json(fixture.project());

    let source = fixture.project().join("fresh-source.md");
    fs::write(
        &source,
        "# Fresh source\n\nFresh project compile should select this ingested note.\n",
    )
    .expect("write source");

    let init = gwiki(
        &fixture,
        fixture.project(),
        &["--format", "json", "init", "--project"],
    );
    common::assert_success(&init, "project init");

    let source_arg = source.to_str().expect("source path utf8");
    let ingest = gwiki(
        &fixture,
        fixture.project(),
        &[
            "--format",
            "json",
            "--project",
            "ingest-file",
            "--no-ai",
            source_arg,
        ],
    );
    common::assert_success(&ingest, "ingest-file");
    let ingest_payload = common::json_stdout(&ingest);
    let source_id = ingest_payload["source"]["id"]
        .as_str()
        .expect("source id")
        .to_string();

    let compile = gwiki(
        &fixture,
        fixture.project(),
        &[
            "--format",
            "json",
            "--project",
            "compile",
            "Fresh Vault Compile",
            "--source",
            &source_id,
            "--ai",
            "off",
        ],
    );
    common::assert_success(&compile, "compile");
    let compile_payload = common::json_stdout(&compile);
    assert_eq!(compile_payload["command"], "compile");
    assert_eq!(compile_payload["ai"]["route"], "off");

    let vault = fixture.project().join("gobby-wiki");
    let article = vault.join("knowledge/topics/fresh-vault-compile.md");
    assert!(article.is_file(), "missing article {}", article.display());

    let provenance_path = vault.join("meta/provenance.json");
    assert!(
        provenance_path.is_file(),
        "missing provenance {}",
        provenance_path.display()
    );
    let provenance = fs::read_to_string(&provenance_path).expect("read provenance");
    assert!(provenance.contains(&format!("raw/{source_id}.md")));

    let checkpoint_path = vault.join("_gwiki/research-session.json");
    assert!(
        checkpoint_path.is_file(),
        "missing checkpoint {}",
        checkpoint_path.display()
    );
    let checkpoint: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(checkpoint_path).expect("read checkpoint"))
            .expect("checkpoint JSON");
    assert_eq!(checkpoint["question"], "Fresh Vault Compile");
    assert_eq!(
        checkpoint["accepted_notes"][0]["path"],
        format!("raw/{source_id}.md")
    );
}
