//! Golden adapters (design-v2 section 7.11, H2).
//!
//! Each adapter loads an existing fixture file unchanged and replays it through the v3 driver.
//! No fixture is re-recorded and no fixture byte changes.

use std::path::{Path, PathBuf};

use gobby_core::grant::presentation::signature_matches;
use gobby_core::grant::{parse_grant_json, verify_payload_checksum};
use serde_json::Value;

use crate::common::{TIMEOUT, frame, read_exact_into, start_front_door, ws_backend, ws_client};

/// The signing secret the Python golden corpus signs with (`tests/runtime_grants/support.py`).
/// Built from parts so the literal is not a credential-shaped string; the guard test below reads
/// the Python source and fails if the two drift.
const GOLDEN_SECRET: &str = concat!("golden-grant-", "signing-secret");

/// A final, unmasked text frame: the server's frame header for one text payload.
const TEXT_FIN: u8 = 0x81;

/// How one grant golden file replays through gcore's public checks.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GrantOutcome {
    /// Every check passed: the parse, the canonical round trip, the payload checksum and the
    /// signature.
    Accepted,
    /// A check refused the grant with this rejection class.
    Rejected(String),
}

fn fixtures_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures")
}

fn grant_golden_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/runtime_grants/golden")
}

fn read_value(path: &Path) -> Value {
    serde_json::from_slice(&std::fs::read(path).expect("read json")).expect("parse json")
}

/// Replay every terminal WS golden fixture, one connection each, through the front door. Returns
/// each fixture's name with `Ok` when its replayed bytes equal the fixture bytes.
pub async fn replay_terminal_ws_fixtures() -> Vec<(String, Result<(), String>)> {
    let dir = fixtures_dir().join("terminal_ws_golden");
    let manifest = read_value(&dir.join("manifest.json"));
    let mut results = Vec::new();
    for name in manifest["fixtures"].as_array().expect("fixtures array") {
        let name = name.as_str().expect("fixture name").to_owned();
        let payload = std::fs::read(dir.join(&name)).expect("read fixture");
        let outcome = replay_terminal_fixture(&payload).await;
        results.push((name, outcome));
    }
    results
}

/// One fixture: one connection through the front door, against a stub that echoes the fixture as
/// one text frame. Both legs are compared byte for byte, as `ws_golden_proxy.rs` compares the
/// whole corpus.
async fn replay_terminal_fixture(payload: &[u8]) -> Result<(), String> {
    let client = frame(TEXT_FIN, payload, true);
    let reply = frame(TEXT_FIN, payload, false);
    let (backend, captured) = ws_backend(client.len(), reply.clone()).await;
    let door = start_front_door(backend).await;
    let (mut stream, head, mut received) = ws_client(door, "/api/terminals/ws").await;
    if !head.starts_with("HTTP/1.1 101") {
        return Err(format!(
            "upgrade refused: {}",
            head.lines().next().unwrap_or_default()
        ));
    }
    tokio::io::AsyncWriteExt::write_all(&mut stream, &client)
        .await
        .map_err(|error| error.to_string())?;
    tokio::time::timeout(
        TIMEOUT,
        read_exact_into(&mut stream, &mut received, reply.len()),
    )
    .await
    .map_err(|_| "the reply timed out".to_owned())?;
    let capture = tokio::time::timeout(TIMEOUT, captured)
        .await
        .map_err(|_| "the backend capture timed out".to_owned())?
        .map_err(|_| "the backend capture was dropped".to_owned())?;
    if capture.received != client {
        return Err("the client-to-backend leg changed the fixture bytes".to_owned());
    }
    if received != reply {
        return Err("the backend-to-client leg changed the fixture bytes".to_owned());
    }
    Ok(())
}

/// Replay the shared envelope corpus (`ws_envelope_golden::replay_corpus`) and return the number
/// of cases it replayed.
pub fn replay_envelope_corpus() -> usize {
    crate::ws_envelope_golden::replay_corpus()
}

/// Replay each runtime grant golden file through gcore's public checks, in file-name order.
pub fn replay_grant_goldens() -> Vec<(String, GrantOutcome)> {
    let dir = grant_golden_dir();
    let mut names: Vec<String> = std::fs::read_dir(&dir)
        .expect("golden dir")
        .map(|entry| {
            entry
                .expect("golden entry")
                .file_name()
                .to_string_lossy()
                .into_owned()
        })
        .filter(|name| name.ends_with(".json"))
        .collect();
    names.sort();
    names
        .into_iter()
        .map(|name| {
            let raw = std::fs::read(dir.join(&name)).expect("read golden");
            let outcome = grant_outcome(&raw);
            (name, outcome)
        })
        .collect()
}

/// The checks `golden_vectors_match_python` runs, through gcore's public functions: the parse
/// (which refuses an unknown field as `payload_skew`), the canonical round trip, the payload
/// checksum and the signature. The first refusal is the rejection class.
fn grant_outcome(raw: &[u8]) -> GrantOutcome {
    let grant = match parse_grant_json(raw) {
        Ok(grant) => grant,
        Err(error) => return GrantOutcome::Rejected(error.cli_code().to_owned()),
    };
    match grant.model_dump_canonical() {
        Ok(canonical) if canonical == raw => {}
        Ok(_) => return GrantOutcome::Rejected("canonical".to_owned()),
        Err(error) => return GrantOutcome::Rejected(error.cli_code().to_owned()),
    }
    if let Err(error) = verify_payload_checksum(&grant) {
        return GrantOutcome::Rejected(error.cli_code().to_owned());
    }
    if !signature_matches(&grant, GOLDEN_SECRET.as_bytes()) {
        return GrantOutcome::Rejected("signature".to_owned());
    }
    GrantOutcome::Accepted
}

#[tokio::test]
async fn terminal_ws_44_replay() {
    let manifest = read_value(&fixtures_dir().join("terminal_ws_golden/manifest.json"));
    let listed = manifest["fixtures"].as_array().expect("fixtures").len();
    assert_eq!(listed, 44, "the terminal golden manifest lists 44 fixtures");
    let results = replay_terminal_ws_fixtures().await;
    assert_eq!(results.len(), listed, "every manifest fixture replays");
    for (name, outcome) in results {
        assert_eq!(
            outcome,
            Ok(()),
            "{name}: replayed bytes differ from the fixture"
        );
    }
}

#[test]
fn envelope_18_replay() {
    let cases = read_value(&fixtures_dir().join("ws_envelope_golden/cases.json"));
    let listed = cases
        .as_array()
        .or_else(|| cases["cases"].as_array())
        .expect("envelope cases")
        .len();
    assert_eq!(listed, 18, "the envelope corpus has 18 cases");
    assert_eq!(
        replay_envelope_corpus(),
        listed,
        "every envelope case replays"
    );
}

#[test]
fn grant_goldens_replay() {
    let python = std::fs::read_to_string(grant_golden_dir().join("../support.py"))
        .expect("the Python grant support module");
    assert!(
        python.contains(&format!("GOLDEN_SECRET = \"{GOLDEN_SECRET}\"")),
        "the golden secret drifted from tests/runtime_grants/support.py"
    );
    let dir = grant_golden_dir();
    let mut names: Vec<String> = std::fs::read_dir(&dir)
        .expect("golden dir")
        .map(|entry| {
            entry
                .expect("golden entry")
                .file_name()
                .to_string_lossy()
                .into_owned()
        })
        .filter(|name| name.ends_with(".json"))
        .collect();
    names.sort();
    assert_eq!(names.len(), 6, "the grant goldens are six files");
    let results = replay_grant_goldens();
    let replayed: Vec<String> = results.iter().map(|(name, _)| name.clone()).collect();
    assert_eq!(
        replayed, names,
        "every golden file replays, in directory order"
    );
    for (name, outcome) in results {
        // The Python golden tests name these two outcomes: the skewed payload is rejected as
        // payload_skew (test_payload_skew_unknown_field_golden_rejects) and the old client's grant
        // as api_contract_mismatch (test_present_old_client_new_grant_rejects_api_contract).
        let expected = match name.as_str() {
            "payload_skew_unknown_field.json" => GrantOutcome::Rejected("payload_skew".to_owned()),
            "old_client_new_grant.json" => {
                GrantOutcome::Rejected("api_contract_mismatch".to_owned())
            }
            _ => GrantOutcome::Accepted,
        };
        assert_eq!(outcome, expected, "{name}");
    }
}
