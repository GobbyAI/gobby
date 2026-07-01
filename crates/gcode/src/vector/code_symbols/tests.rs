use super::*;
use crate::config::{
    CODE_SYMBOL_COLLECTION_PREFIX, CodeVectorSettings, Context, EmbeddingConfig, QdrantConfig,
};
use crate::models::{ProjectionProvenance, SOURCE_SYSTEM_GCODE, Symbol};
use serde_json::Value;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::thread;
use std::time::{Duration, Instant};

mod collection;
mod deletion;
mod embedding;
mod module_scope;
mod payload;
mod sync;

fn test_symbol(summary: Option<String>) -> Symbol {
    Symbol {
        id: "symbol-1".to_string(),
        project_id: "project-1".to_string(),
        file_path: "src/lib.rs".to_string(),
        name: "run".to_string(),
        qualified_name: "crate::run".to_string(),
        kind: "function".to_string(),
        language: "rust".to_string(),
        byte_start: 10,
        byte_end: 40,
        line_start: 3,
        line_end: 5,
        signature: None,
        docstring: None,
        parent_symbol_id: None,
        content_hash: "hash".to_string(),
        summary,
        created_at: String::new(),
        updated_at: String::new(),
    }
}

fn test_symbol_with_index(index: usize) -> Symbol {
    let mut symbol = test_symbol(None);
    symbol.id = format!("symbol-{index}");
    symbol.name = format!("run_{index}");
    symbol.qualified_name = format!("crate::run_{index}");
    symbol.byte_start = index;
    symbol.byte_end = index + 1;
    symbol
}

fn test_context(qdrant: Option<QdrantConfig>) -> Context {
    Context {
        database_url: "postgresql://localhost/nonexistent".to_string(),
        project_root: PathBuf::from("/nonexistent"),
        project_id: "project-1".to_string(),
        quiet: true,
        falkordb: None,
        qdrant,
        embedding: None,
        code_vectors: CodeVectorSettings::default(),
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        index_scope: crate::config::ProjectIndexScope::Single,
    }
}

fn spawn_http_responses(responses: Vec<(u16, Value)>) -> (String, thread::JoinHandle<Vec<String>>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("set test server nonblocking");
    let addr = listener.local_addr().expect("local addr");
    let handle = thread::spawn(move || {
        let mut requests = Vec::new();
        for (status, body) in responses {
            let mut stream =
                accept_with_timeout(&listener, Duration::from_secs(5)).expect("accept request");
            requests.push(read_http_request(&mut stream));

            let reason = reqwest::StatusCode::from_u16(status)
                .ok()
                .and_then(|status| status.canonical_reason())
                .unwrap_or("OK");
            let body = body.to_string();
            write!(
                    stream,
                    "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                )
                .expect("write response");
        }
        requests
    });

    (format!("http://{addr}"), handle)
}

fn accept_with_timeout(listener: &TcpListener, timeout: Duration) -> std::io::Result<TcpStream> {
    let deadline = Instant::now() + timeout;
    loop {
        match listener.accept() {
            Ok((stream, _)) => {
                stream.set_nonblocking(false)?;
                return Ok(stream);
            }
            Err(err) if err.kind() == std::io::ErrorKind::WouldBlock => {
                if Instant::now() >= deadline {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::TimedOut,
                        "timed out waiting for test HTTP request",
                    ));
                }
                thread::sleep(Duration::from_millis(10));
            }
            Err(err) => return Err(err),
        }
    }
}

fn read_http_request(stream: &mut TcpStream) -> String {
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .expect("set read timeout");
    // Stop at Content-Length when present; otherwise the read timeout ends
    // keep-alive or bodyless test requests without hanging the server thread.
    let mut request = Vec::new();
    let mut buffer = [0; 4096];
    let mut expected_len = None;

    loop {
        let n = stream.read(&mut buffer).expect("read request");
        if n == 0 {
            break;
        }
        request.extend_from_slice(&buffer[..n]);

        if expected_len.is_none()
            && let Some(header_end) = request.windows(4).position(|window| window == b"\r\n\r\n")
        {
            let headers = String::from_utf8_lossy(&request[..header_end]);
            let content_len = headers
                .lines()
                .find_map(|line| {
                    line.to_ascii_lowercase()
                        .strip_prefix("content-length: ")
                        .and_then(|value| value.parse::<usize>().ok())
                })
                .unwrap_or(0);
            expected_len = Some(header_end + 4 + content_len);
        }

        if let Some(expected_len) = expected_len
            && request.len() >= expected_len
        {
            break;
        }
    }

    String::from_utf8_lossy(&request).into_owned()
}
