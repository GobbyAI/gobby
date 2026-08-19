use std::fs;
use std::path::{Path, PathBuf};
use std::process::Output;

#[path = "../common/mod.rs"]
mod common;

mod benchmark;
mod config_postgres;
mod ingest_refresh;
mod public_smoke;

use gobby_wiki::session::{AcceptedResearchNote, ResearchScope, ResearchSession};
use gobby_wiki::sources::{
    CompileStatus, IngestionMethod, SourceDraft, SourceKind, SourceManifest,
};

fn gwiki(fixture: &common::GwikiFixture, cwd: &Path, args: &[&str]) -> Output {
    fixture
        .command_in(cwd)
        .args(args)
        .output()
        .expect("gwiki binary runs")
}

fn assert_daemon_required(output: &Output, label: &str) {
    common::assert_daemon_required(output, label);
}

fn gwiki_with_database_url(
    fixture: &common::GwikiFixture,
    cwd: &Path,
    database_url: &str,
    args: &[&str],
) -> Output {
    fixture
        .command_with_database_url_in(cwd, database_url)
        .args(args)
        .env("GWIKI_ALLOW_LOOPBACK_URL_FETCH_FOR_TESTS", "1")
        .output()
        .expect("gwiki binary runs")
}

fn assert_json_path(value: &serde_json::Value, expected: &Path) {
    let actual = value.as_str().expect("path string");
    assert_eq!(
        comparable_test_path(Path::new(actual)),
        comparable_test_path(expected),
        "{value:#}"
    );
}

fn comparable_test_path(path: &Path) -> PathBuf {
    path.canonicalize().unwrap_or_else(|_| path.to_path_buf())
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
            last_verified_at: "2026-05-30T00:00:00Z".to_string(),
            fetch_provenance: gobby_wiki::sources::FetchProvenance::Stub,
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
        code_citations: Vec::new(),
        degradation: None,
    });
    session.save_checkpoint().expect("save checkpoint");
}
