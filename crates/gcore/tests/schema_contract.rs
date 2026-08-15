#![cfg(feature = "postgres")]

use gobby_core::schema::{
    BASELINE_CHECKSUM, BASELINE_VERSION, RUNNER_PROTOCOL_VERSION, SEED_MANIFEST_JSON, SchemaRunner,
    parse_backup_manifest, schema_identity, split_sql_statements,
};

#[test]
fn embedded_assets_publish_a_complete_schema_identity() {
    let identity = schema_identity();

    assert_eq!(BASELINE_VERSION, 375);
    assert_eq!(
        BASELINE_CHECKSUM,
        "5d598c3609d0bdbcfd10f1c363c60bd38d5625100c3e8719b3ff42d189047117"
    );
    assert_eq!(identity.runner_protocol_version, RUNNER_PROTOCOL_VERSION);
    assert_eq!(identity.baseline.version, BASELINE_VERSION);
    assert_eq!(identity.baseline.checksum, BASELINE_CHECKSUM);
    assert_eq!(identity.latest_asset.version, 381);
    assert_eq!(
        identity.latest_asset.filename,
        "381_drop_legacy_workflow_tables.sql"
    );
    assert_eq!(
        identity.latest_asset.checksum,
        "029f44aeeaf260d617e981ec77a558f21e2f8cb1af2e49e1d14adbc3458cc2e8"
    );
    assert_eq!(
        identity.root_hash,
        "dc2d7235b42d04ce4566d2e849c90493fa09dc98e557cf19ee51fe4e4a4e5475"
    );

    let _public_runner_type = std::any::type_name::<SchemaRunner<'static>>();
}

#[test]
fn embedded_seed_preserves_project_without_machine_less_system_session() {
    let manifest: serde_json::Value =
        serde_json::from_str(SEED_MANIFEST_JSON).expect("seed manifest must be valid JSON");
    assert_eq!(manifest["sessions"], serde_json::json!([]));
    assert!(manifest["projects"].as_array().is_some_and(|projects| {
        projects
            .iter()
            .any(|project| project["values"]["name"] == "_personal")
    }));
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
    assert_eq!(manifest.backup_starting_head, 376);
    assert_eq!(manifest.stores.len(), 4);

    let round_trip = serde_json::to_string(&manifest).expect("serializable manifest");
    let reparsed = parse_backup_manifest(&round_trip).expect("Rust-produced v2 manifest");
    assert_eq!(reparsed, manifest);
}
