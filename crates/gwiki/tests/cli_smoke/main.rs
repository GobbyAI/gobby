use std::fs;
use std::io::ErrorKind;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::path::Path;
use std::process::Output;
use std::thread;
use std::time::{Duration, Instant};

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

fn http_fixture_timeout() -> Duration {
    std::env::var("GWIKI_TEST_HTTP_TIMEOUT_SECS")
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
        .map(Duration::from_secs)
        .unwrap_or_else(|| Duration::from_secs(15))
}

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
    listener
        .set_nonblocking(true)
        .expect("configure nonblocking HTTP fixture");
    let base_url = format!("http://{}", listener.local_addr().expect("local addr"));
    let timeout = http_fixture_timeout();
    let handle = thread::spawn(move || {
        for (status, body) in responses {
            let deadline = Instant::now() + timeout;
            let (mut stream, _) = loop {
                match listener.accept() {
                    Ok(connection) => break connection,
                    Err(error) if error.kind() == ErrorKind::WouldBlock => {
                        assert!(
                            Instant::now() < deadline,
                            "timed out waiting for HTTP fixture request"
                        );
                        thread::sleep(Duration::from_millis(10));
                    }
                    Err(error) => panic!("accept HTTP fixture request: {error}"),
                }
            };
            stream
                .set_nonblocking(false)
                .expect("configure blocking HTTP fixture stream");
            let mut buffer = [0_u8; 1024];
            let mut request = Vec::new();
            loop {
                let bytes_read = stream.read(&mut buffer).expect("read HTTP fixture request");
                if bytes_read == 0 {
                    break;
                }
                request.extend_from_slice(&buffer[..bytes_read]);
                if request.windows(4).any(|window| window == b"\r\n\r\n") {
                    break;
                }
            }
            assert!(
                request.windows(4).any(|window| window == b"\r\n\r\n"),
                "HTTP fixture request headers should be complete"
            );
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
        code_citations: Vec::new(),
        degradation: None,
    });
    session.save_checkpoint().expect("save checkpoint");
}
