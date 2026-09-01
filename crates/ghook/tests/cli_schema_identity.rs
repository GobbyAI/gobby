use std::collections::BTreeSet;
use std::process::Command;

use serde_json::Value;

#[test]
fn schema_identity_json_reports_exact_contract() {
    let output = Command::new(env!("CARGO_BIN_EXE_ghook"))
        .args(["schema-identity", "--json"])
        .output()
        .expect("run ghook schema-identity");

    assert!(
        output.status.success(),
        "ghook schema-identity failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );

    assert_schema_identity(&output.stdout);
}

fn assert_schema_identity(stdout: &[u8]) {
    let actual: Value = serde_json::from_slice(stdout).expect("schema identity stdout json");
    let object = actual.as_object().expect("schema identity object");
    let keys = object.keys().map(String::as_str).collect::<BTreeSet<_>>();
    assert_eq!(
        keys,
        BTreeSet::from([
            "assets_root_hash",
            "baseline_checksum",
            "baseline_version",
            "latest_checksum",
            "latest_version",
            "runner_protocol",
        ])
    );

    let embedded = gobby_core::schema::schema_identity();
    assert_eq!(actual["runner_protocol"], embedded.runner_protocol_version);
    assert_eq!(actual["baseline_version"], embedded.baseline.version);
    assert_eq!(actual["baseline_checksum"], embedded.baseline.checksum);
    assert_eq!(actual["latest_version"], embedded.latest_asset.version);
    assert_eq!(actual["latest_checksum"], embedded.latest_asset.checksum);
    assert_eq!(actual["assets_root_hash"], embedded.root_hash);
}
