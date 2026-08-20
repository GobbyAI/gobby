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
        "30eb60a384d201dc42bb4ffb78d18eb2ce6209200ac1f47708e48ac21a792944"
    );
    assert_eq!(identity.runner_protocol_version, RUNNER_PROTOCOL_VERSION);
    assert_eq!(identity.baseline.version, BASELINE_VERSION);
    assert_eq!(identity.baseline.checksum, BASELINE_CHECKSUM);
    assert_eq!(identity.latest_asset.version, 398);
    assert_eq!(
        identity.latest_asset.filename,
        "398_code_indexed_project_states_indexer_version.sql"
    );
    assert_eq!(
        identity.latest_asset.checksum,
        "1dbe00b375b252d2ab283e1a0c1ef66f5bcd79b1f2a9618a4452f7a1ab153da2"
    );
    assert_eq!(
        identity.root_hash,
        "aff9e87d2fcca7eac3ab7ccb0ffad97d0f04625cf1ca4f39da392fb29a6fb2ff"
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
fn python_backup_manifest_v3_fixture_round_trips() {
    let fixture = include_str!("fixtures/hub_backup_manifest/v3_roundtrip.json");
    let manifest = parse_backup_manifest(fixture).expect("valid Python-produced v3 manifest");

    assert_eq!(manifest.manifest_format, "gobby-hub-backup-manifest");
    assert_eq!(manifest.manifest_version, 3);
    assert_eq!(manifest.backup_starting_head, 376);
    assert_eq!(manifest.stores.len(), 5);
    assert!(manifest.stores.contains_key("files"));

    let round_trip = serde_json::to_string(&manifest).expect("serializable manifest");
    let reparsed = parse_backup_manifest(&round_trip).expect("Rust-produced v3 manifest");
    assert_eq!(reparsed, manifest);
}
