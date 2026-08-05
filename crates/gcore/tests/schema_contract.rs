#![cfg(feature = "postgres")]

use gobby_core::schema::{
    BASELINE_CHECKSUM, BASELINE_VERSION, RUNNER_PROTOCOL_VERSION, SEED_MANIFEST_JSON, SchemaRunner,
    parse_backup_manifest, schema_identity, split_sql_statements,
};

fn repo_file(path: &str) -> String {
    std::fs::read_to_string(
        std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../..")
            .join(path),
    )
    .unwrap_or_else(|error| panic!("failed to read {path}: {error}"))
}

#[test]
fn embedded_assets_publish_a_complete_schema_identity() {
    let identity = schema_identity();

    assert_eq!(BASELINE_VERSION, 375);
    assert_eq!(
        BASELINE_CHECKSUM,
        "eaf97c2662053cf0f3b112410d66b7bc123402f1100224873656bbe199bc7a80"
    );
    assert_eq!(identity.runner_protocol_version, RUNNER_PROTOCOL_VERSION);
    assert_eq!(identity.baseline.version, BASELINE_VERSION);
    assert_eq!(identity.baseline.checksum, BASELINE_CHECKSUM);
    assert_eq!(identity.latest_asset, identity.baseline);
    assert_eq!(identity.root_hash.len(), 64);

    let _public_runner_type = std::any::type_name::<SchemaRunner<'static>>();
}

#[test]
fn embedded_seed_matches_the_flatten_evidence() {
    assert_eq!(
        SEED_MANIFEST_JSON,
        repo_file("docs/evidence/pre-flatten/migrated-fresh.seed.json")
    );
}

#[test]
fn statement_splitter_preserves_dollar_quoted_bodies() {
    let sql = "CREATE FUNCTION f() RETURNS void AS $body$ BEGIN PERFORM ';'; END $body$ LANGUAGE plpgsql; SELECT 1;";

    let statements = split_sql_statements(sql).expect("valid SQL script");

    assert_eq!(statements.len(), 2);
    assert!(statements[0].contains("PERFORM ';'"));
    assert_eq!(statements[1].trim(), "SELECT 1");
}

#[test]
fn python_backup_manifest_v2_fixture_round_trips() {
    let fixture = include_str!("fixtures/hub_backup_manifest/v2_roundtrip.json");
    let manifest = parse_backup_manifest(fixture).expect("valid Python-produced v2 manifest");

    assert_eq!(manifest.manifest_format, "gobby-hub-backup-manifest");
    assert_eq!(manifest.manifest_version, 2);
    assert_eq!(manifest.backup_starting_head, BASELINE_VERSION);
    assert_eq!(manifest.stores.len(), 4);

    let round_trip = serde_json::to_string(&manifest).expect("serializable manifest");
    let reparsed = parse_backup_manifest(&round_trip).expect("Rust-produced v2 manifest");
    assert_eq!(reparsed, manifest);
}
