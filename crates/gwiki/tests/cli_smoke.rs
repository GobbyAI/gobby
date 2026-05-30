use std::fs;
use std::path::Path;
use std::process::{Command, Output};

use gobby_wiki::session::{AcceptedResearchNote, ResearchScope, ResearchSession};
use gobby_wiki::sources::{
    CompileStatus, IngestionMethod, SourceDraft, SourceKind, SourceManifest,
};

fn gwiki(hub: &Path, cwd: &Path, args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_gwiki"))
        .args(args)
        .env("GOBBY_WIKI_HUB", hub)
        .env("HOME", cwd)
        .env_remove("GWIKI_DATABASE_URL")
        .env_remove("GOBBY_POSTGRES_DSN")
        .env_remove("GCODE_DATABASE_URL")
        .current_dir(cwd)
        .output()
        .expect("gwiki binary runs")
}

fn assert_success(output: &Output, label: &str) {
    assert!(
        output.status.success(),
        "{label} failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}

fn json_output(output: &Output) -> serde_json::Value {
    serde_json::from_slice(&output.stdout).expect("stdout is JSON")
}

fn assert_json_path(value: &serde_json::Value, expected: &Path) {
    assert_eq!(
        value.as_str(),
        Some(expected.to_str().expect("path utf8")),
        "{value:#}"
    );
}

fn seed_accepted_research_checkpoint(vault: &Path) {
    let note_path = vault.join("raw/research/ownership-evidence.md");
    fs::create_dir_all(note_path.parent().expect("note parent")).expect("create research dir");
    fs::write(
        &note_path,
        r#"---
title: Ownership evidence
indexable: true
---

Ownership evidence is grounded in accepted research notes.
citation: Rust Reference, Ownership
"#,
    )
    .expect("write accepted research note");
    SourceManifest::register(
        vault,
        SourceDraft {
            location: "raw/research/ownership-evidence.md".to_string(),
            kind: SourceKind::ResearchNote,
            fetched_at: "2026-05-30T00:00:00Z".to_string(),
            content: b"Ownership evidence is grounded in accepted research notes.".to_vec(),
            title: Some("Ownership evidence".to_string()),
            citation: Some("Rust Reference, Ownership".to_string()),
            license: None,
            ingestion_method: IngestionMethod::Research,
            compile_status: CompileStatus::Pending,
        },
    )
    .expect("register research source");

    let mut session = ResearchSession::new(
        "How should ownership evidence compile?",
        ResearchScope::topic("rust", vault),
        Vec::new(),
        1,
        Some("#306".to_string()),
    )
    .expect("research session");
    session.accepted_notes.push(AcceptedResearchNote {
        title: "Ownership evidence".to_string(),
        path: note_path,
    });
    session.save_checkpoint().expect("save checkpoint");
}

#[test]
fn command_modules_do_not_define_static_placeholder_results() {
    let commands_dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("src/commands");
    let placeholder_patterns = [
        "\"objects\": []",
        "\"created\": []",
        "\"results\": []",
        "\"backlinks\": []",
        "\"suggestions\": []",
        "\"documents\": 0",
        "\"chunks\": 0",
        "\"links\": 0",
        "Init ready",
        "Setup ready",
        "Index ready",
        "Ingest file ready",
    ];

    for entry in fs::read_dir(commands_dir).expect("read commands dir") {
        let path = entry.expect("read command entry").path();
        if path.extension().and_then(|extension| extension.to_str()) != Some("rs") {
            continue;
        }

        let source = fs::read_to_string(&path).expect("read command source");
        for pattern in placeholder_patterns {
            assert!(
                !source.contains(pattern),
                "{} still contains placeholder pattern {pattern:?}",
                path.display()
            );
        }
    }
}

#[test]
fn public_cli_smoke_uses_gwiki_modules() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let hub = tmp.path().join("hub");
    let source = tmp.path().join("ownership-source.md");
    fs::write(
        &source,
        "# Ownership Source\n\nOwnership evidence for Rust borrowing.\n",
    )
    .expect("write source");

    let init = gwiki(
        &hub,
        tmp.path(),
        &["--format", "json", "init", "--topic", "rust"],
    );
    assert_success(&init, "init");

    let setup = gwiki(
        &hub,
        tmp.path(),
        &["--format", "json", "setup", "--topic", "rust"],
    );
    assert_success(&setup, "setup");
    let setup_payload = json_output(&setup);
    assert!(
        setup_payload["objects"]
            .as_array()
            .is_some_and(|objects| !objects.is_empty()),
        "{setup_payload:#}"
    );

    let vault = hub.join("topics").join("rust");
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
        &hub,
        tmp.path(),
        &[
            "--format",
            "json",
            "ingest-file",
            "--topic",
            "rust",
            source.to_str().expect("source path utf8"),
        ],
    );
    assert_success(&ingest, "ingest-file");
    let ingest_payload = json_output(&ingest);
    assert_eq!(ingest_payload["command"], "ingest-file");
    assert!(ingest_payload["raw_path"].as_str().is_some());

    let index = gwiki(
        &hub,
        tmp.path(),
        &["--format", "json", "index", "--topic", "rust"],
    );
    assert_success(&index, "index");
    let index_payload = json_output(&index);
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
        &hub,
        tmp.path(),
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
    assert_success(&search, "search");
    let search_payload = json_output(&search);
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
        &hub,
        tmp.path(),
        &[
            "--format",
            "json",
            "backlinks",
            "--topic",
            "rust",
            "wiki/topics/ownership.md",
        ],
    );
    assert_success(&backlinks, "backlinks");
    let backlinks_payload = json_output(&backlinks);
    assert!(
        backlinks_payload["backlinks"]
            .as_array()
            .is_some_and(|links| !links.is_empty()),
        "{backlinks_payload:#}"
    );

    let suggestions = gwiki(
        &hub,
        tmp.path(),
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
    assert_success(&suggestions, "link-suggest");
    let suggestions_payload = json_output(&suggestions);
    assert!(
        suggestions_payload["suggestions"]
            .as_array()
            .is_some_and(|suggestions| !suggestions.is_empty()),
        "{suggestions_payload:#}"
    );

    seed_accepted_research_checkpoint(&vault);

    let research = gwiki(
        &hub,
        tmp.path(),
        &[
            "--format",
            "json",
            "--topic",
            "rust",
            "research",
            "--resume",
            "How should ownership evidence compile?",
        ],
    );
    assert_success(&research, "research");
    let research_payload = json_output(&research);
    assert_eq!(research_payload["command"], "research");
    assert_json_path(&research_payload["session"]["scope"]["root"], &vault);

    let compile = gwiki(
        &hub,
        tmp.path(),
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
    assert_success(&compile, "compile");
    let compile_payload = json_output(&compile);
    assert_eq!(compile_payload["command"], "compile");
    assert_json_path(
        &compile_payload["article_path"],
        &vault.join("wiki/topics/ownership-synthesis.md"),
    );
    assert!(vault.join("wiki/sources/ownership-evidence.md").is_file());

    let audit = gwiki(
        &hub,
        tmp.path(),
        &["--format", "json", "--topic", "rust", "audit"],
    );
    assert_success(&audit, "audit");
    let audit_payload = json_output(&audit);
    assert_eq!(audit_payload["command"], "audit");
    assert_json_path(&audit_payload["root"], &vault);
}
