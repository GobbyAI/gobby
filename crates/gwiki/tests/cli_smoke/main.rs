use std::fs;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::Path;
use std::process::Output;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::Duration;

#[path = "../common/mod.rs"]
mod common;

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
        .env("GWIKI_ALLOW_LOOPBACK_URL_FETCH_FOR_TESTS", "1")
        .output()
        .expect("gwiki binary runs")
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
    assert_eq!(
        value.as_str(),
        Some(expected.to_str().expect("path utf8")),
        "{value:#}"
    );
}

fn serve_http_responses(
    responses: Vec<(&'static str, &'static str)>,
) -> (String, thread::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind HTTP fixture");
    let base_url = format!("http://{}", listener.local_addr().expect("local addr"));
    let wake_addr = listener.local_addr().expect("local addr");
    let handle = thread::spawn(move || {
        for (status, body) in responses {
            let accepted = Arc::new(AtomicBool::new(false));
            let timed_out = Arc::new(AtomicBool::new(false));
            let watchdog_accepted = Arc::clone(&accepted);
            let watchdog_timed_out = Arc::clone(&timed_out);
            thread::spawn(move || {
                thread::sleep(Duration::from_secs(5));
                if !watchdog_accepted.load(Ordering::Acquire) {
                    watchdog_timed_out.store(true, Ordering::Release);
                    let _ = TcpStream::connect(wake_addr);
                }
            });
            let (mut stream, _) = listener.accept().expect("accept HTTP fixture request");
            accepted.store(true, Ordering::Release);
            assert!(
                !timed_out.load(Ordering::Acquire),
                "timed out waiting for HTTP fixture request"
            );
            let mut buffer = [0_u8; 1024];
            let bytes_read = stream.read(&mut buffer).expect("read HTTP fixture request");
            assert!(bytes_read > 0, "HTTP fixture request should not be empty");
            let response = format!(
                "HTTP/1.1 {status}\r\nContent-Type: text/html\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            stream
                .write_all(response.as_bytes())
                .expect("write HTTP fixture response");
        }
    });
    (base_url, handle)
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
