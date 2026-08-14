//! Plan 3.1 contract tests: daemon-only routing, grant modality gates, and
//! workspace zero-match audits for removed Direct/Auto surfaces.

use std::fs;
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::thread;
use std::time::{Duration, Instant};

use crate::ai_types::AiError;
use crate::config::{AiCapability, AiRouting};
use crate::grant::{
    AiCapability as GrantAiCapability, FalkorCapability, GrantCapabilities, PostgresCapability,
    QdrantCapability,
};

fn forbidden_routing() -> [String; 3] {
    // Concatenate at runtime so the E1 zero-match greps stay empty.
    [
        format!("{}::{}", "AiRouting", "Direct"),
        format!("{}::{}", "AiRouting", "Auto"),
        format!("Direct{}Transport", "Chat"),
    ]
}

fn vendor_env_keys() -> [String; 4] {
    ["ANTHROPIC", "OPENAI", "OPENROUTER", "GROQ"].map(|vendor| format!("{vendor}_API_KEY"))
}

fn secret_marker() -> String {
    format!("${}:", "secret")
}

fn crate_src() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("src")
}

fn workspace_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn this_test_file() -> PathBuf {
    crate_src()
        .join("ai/tests.rs")
        .canonicalize()
        .unwrap_or_else(|_| crate_src().join("ai/tests.rs"))
}

fn walk_rust_files(root: &Path, out: &mut Vec<PathBuf>) {
    let entries = match fs::read_dir(root) {
        Ok(entries) => entries,
        Err(_) => return,
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            if path.file_name().and_then(|name| name.to_str()) == Some("target") {
                continue;
            }
            walk_rust_files(&path, out);
            continue;
        }
        if path.extension().and_then(|ext| ext.to_str()) == Some("rs") {
            out.push(path);
        }
    }
}

fn client_crate_rust_files() -> Vec<PathBuf> {
    let root = workspace_root();
    let mut files = Vec::new();
    for crate_name in ["gcode", "gcore", "gwiki", "gdaemon", "ghook"] {
        walk_rust_files(&root.join("crates").join(crate_name), &mut files);
    }
    let skip = this_test_file();
    files.retain(|path| {
        path.canonicalize()
            .ok()
            .is_none_or(|canonical| canonical != skip)
    });
    files
}

fn production_source_before_tests(path: &Path) -> String {
    let source = fs::read_to_string(path).unwrap_or_default();
    match source.find("#[cfg(test)]") {
        Some(index) => source[..index].to_string(),
        None => source,
    }
}

fn grant_capabilities(embed: GrantAiCapability) -> GrantCapabilities {
    GrantCapabilities {
        postgres: PostgresCapability::Unavailable {},
        falkordb: FalkorCapability::Unavailable {},
        qdrant: QdrantCapability::Unavailable {},
        embed,
        text_generate: GrantAiCapability::Daemon {},
        tool_chat: GrantAiCapability::Daemon {},
        vision_extract: GrantAiCapability::Daemon {},
        audio_transcribe: GrantAiCapability::Daemon {},
        broker_operations: Vec::new(),
    }
}

#[test]
fn ai_routing_is_daemon_or_off() {
    assert_eq!(
        "daemon".parse::<AiRouting>().expect("daemon"),
        AiRouting::Daemon
    );
    assert_eq!("off".parse::<AiRouting>().expect("off"), AiRouting::Off);
    assert!(
        "auto".parse::<AiRouting>().is_err(),
        "Auto must be unrepresentable"
    );
    assert!(
        "direct".parse::<AiRouting>().is_err(),
        "Direct must be unrepresentable"
    );

    fn label(route: AiRouting) -> &'static str {
        match route {
            AiRouting::Daemon => "daemon",
            AiRouting::Off => "off",
        }
    }
    assert_eq!(label(AiRouting::Daemon), "daemon");
    assert_eq!(label(AiRouting::Off), "off");
}

#[test]
fn probe_module_is_deleted() {
    assert!(
        !crate_src().join("ai/probe.rs").exists(),
        "crates/gcore/src/ai/probe.rs must be deleted"
    );
}

#[test]
fn no_vendor_env_key_reads() {
    let mut hits = Vec::new();
    for path in client_crate_rust_files() {
        let source = production_source_before_tests(&path);
        for key in vendor_env_keys() {
            if source.contains(&key) {
                hits.push(format!("{}: {key}", path.display()));
            }
        }
    }
    assert!(
        hits.is_empty(),
        "client crates must not read vendor API-key environment variables:\n{}",
        hits.join("\n")
    );
}

#[test]
fn runtime_config_contract_has_no_secret_marker() {
    let path = workspace_root().join("crates/gcore/assets/config/runtime_config_contract.json");
    let source = fs::read_to_string(&path).unwrap_or_default();
    assert!(
        !source.contains(&secret_marker()),
        "{} must not carry the client-forbidden secret marker",
        path.display()
    );
}

#[test]
fn workspace_zero_match_removed_routing() {
    let mut hits = Vec::new();
    for path in client_crate_rust_files() {
        let source = fs::read_to_string(&path).unwrap_or_default();
        for needle in forbidden_routing() {
            if source.contains(&needle) {
                hits.push(format!("{}: {needle}", path.display()));
            }
        }
        for key in vendor_env_keys() {
            if source.contains(&key) {
                hits.push(format!("{}: {key}", path.display()));
            }
        }
    }
    assert!(
        hits.is_empty(),
        "workspace still contains removed routing/vendor surfaces:\n{}",
        hits.join("\n")
    );
}

#[test]
fn grant_gates_modalities() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
    let accepts = Arc::new(AtomicUsize::new(0));
    let flag = Arc::clone(&accepts);
    listener.set_nonblocking(true).expect("nonblocking");
    thread::spawn(move || {
        let deadline = Instant::now() + Duration::from_secs(2);
        while Instant::now() < deadline {
            if listener.accept().is_ok() {
                flag.fetch_add(1, Ordering::SeqCst);
            }
            thread::sleep(Duration::from_millis(5));
        }
    });

    let unavailable = grant_capabilities(GrantAiCapability::Unavailable {});
    let error = super::require_modality(&unavailable, AiCapability::Embed)
        .expect_err("unavailable embed must fail typed");
    match error {
        AiError::CapabilityUnavailable { capability, .. } => {
            assert_eq!(capability, "embed");
        }
        other => panic!("expected CapabilityUnavailable, got {other:?}"),
    }

    let reachable = grant_capabilities(GrantAiCapability::Daemon {});
    super::require_modality(&reachable, AiCapability::Embed).expect("daemon embed is permitted");
    let unreachable = super::require_modality_ready(&reachable, false, AiCapability::TextGenerate)
        .expect_err("unreachable daemon must fail typed");
    match unreachable {
        AiError::CapabilityUnavailable {
            capability,
            message,
        } => {
            assert_eq!(capability, "text_generate");
            assert!(
                message.contains("unreachable"),
                "typed error should name the outage: {message}"
            );
        }
        other => panic!("expected CapabilityUnavailable, got {other:?}"),
    }

    thread::sleep(Duration::from_millis(50));
    assert_eq!(
        accepts.load(Ordering::SeqCst),
        0,
        "modality gating must not open an HTTP connection"
    );
}
