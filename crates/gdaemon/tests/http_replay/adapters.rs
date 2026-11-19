//! Golden adapters (design-v2 section 7.11, H2).
//!
//! Each adapter loads an existing fixture file unchanged and replays it through the v3 driver.
//! No fixture is re-recorded and no fixture byte changes.

use std::path::{Path, PathBuf};

use serde_json::Value;

/// How one grant golden file replays through gcore's public checks.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GrantOutcome {
    /// Every check passed: the canonical round trip, the payload checksum and the signature.
    Accepted,
    /// A check refused the grant with this rejection class.
    Rejected(String),
}

fn fixtures_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures")
}

fn read_value(path: &Path) -> Value {
    serde_json::from_slice(&std::fs::read(path).expect("read json")).expect("parse json")
}

/// Replay every terminal WS golden fixture, one connection each, through the front door. Returns
/// each fixture's name with `Ok` when its replayed bytes equal the fixture bytes.
pub async fn replay_terminal_ws_fixtures() -> Vec<(String, Result<(), String>)> {
    unimplemented!("not wired: replay_terminal_ws_fixtures")
}

/// Replay the shared envelope corpus (`ws_envelope_golden::replay_corpus`) and return the number
/// of cases it replayed.
pub fn replay_envelope_corpus() -> usize {
    crate::ws_envelope_golden::replay_corpus()
}

/// Replay each runtime grant golden file through gcore's public checks.
pub fn replay_grant_goldens() -> Vec<(String, GrantOutcome)> {
    unimplemented!("not wired: replay_grant_goldens")
}

#[tokio::test]
async fn terminal_ws_44_replay() {
    let manifest = read_value(&fixtures_dir().join("terminal_ws_golden/manifest.json"));
    let listed = manifest["fixtures"].as_array().expect("fixtures").len();
    assert_eq!(listed, 44, "the terminal golden manifest lists 44 fixtures");
    let results = replay_terminal_ws_fixtures().await;
    assert_eq!(results.len(), listed, "every manifest fixture replays");
    for (name, outcome) in results {
        assert_eq!(outcome, Ok(()), "{name}: replayed bytes differ from the fixture");
    }
}

#[test]
fn envelope_18_replay() {
    let cases = read_value(&fixtures_dir().join("ws_envelope_golden/cases.json"));
    let listed = cases.as_array().or_else(|| cases["cases"].as_array());
    let listed = listed.expect("envelope cases").len();
    assert_eq!(listed, 18, "the envelope corpus has 18 cases");
    assert_eq!(replay_envelope_corpus(), listed, "every envelope case replays");
}

#[test]
fn grant_goldens_replay() {
    let dir = fixtures_dir().join("../runtime_grants/golden");
    let mut names: Vec<String> = std::fs::read_dir(&dir)
        .expect("golden dir")
        .map(|entry| entry.expect("entry").file_name().to_string_lossy().into_owned())
        .filter(|name| name.ends_with(".json"))
        .collect();
    names.sort();
    assert_eq!(names.len(), 6, "the grant goldens are six files");
    let results = replay_grant_goldens();
    let replayed: Vec<String> = results.iter().map(|(name, _)| name.clone()).collect();
    assert_eq!(replayed, names, "every golden file replays, in directory order");
    for (name, outcome) in results {
        let expected = if name == "payload_skew_unknown_field.json" {
            GrantOutcome::Rejected("payload_skew".to_owned())
        } else {
            GrantOutcome::Accepted
        };
        assert_eq!(outcome, expected, "{name}");
    }
}
