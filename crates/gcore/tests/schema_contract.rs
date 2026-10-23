#![cfg(feature = "postgres")]

use gobby_core::schema::{
    BASELINE_CHECKSUM, BASELINE_VERSION, CATALOG_MANIFEST_JSON, RUNNER_PROTOCOL_VERSION,
    SEED_MANIFEST_JSON, SchemaRunner, schema_identity, split_sql_statements,
};

#[test]
fn embedded_assets_publish_a_complete_schema_identity() {
    // Identity pin for the checkout-only project schema after cutover.
    let identity = schema_identity();

    assert_eq!(BASELINE_VERSION, 420);
    assert_eq!(
        BASELINE_CHECKSUM,
        "e5b08e67d3f5df97cfac96b9b12aac1f4f936a9465220f27b95d6e067e207e3a"
    );
    assert_eq!(identity.runner_protocol_version, RUNNER_PROTOCOL_VERSION);
    assert_eq!(identity.baseline.version, BASELINE_VERSION);
    assert_eq!(identity.baseline.checksum, BASELINE_CHECKSUM);
    assert_eq!(identity.latest_asset.version, 475);
    assert_eq!(
        identity.latest_asset.filename,
        "475_recover_revoked_live_agent_principals.sql"
    );
    assert_eq!(
        identity.latest_asset.checksum,
        "17bfb099c85047b3427f1ed8dc828510f62eda43dc869752767425cbb7b46533"
    );
    assert_eq!(
        identity.root_hash,
        "bc1324cdcfa6130bcd847290dcabf6548875e90c8303c564ba52cfea54935387"
    );

    let _public_runner_type = std::any::type_name::<SchemaRunner<'static>>();
}

#[test]
fn catalog_pins_static_plan_review_seat_columns_and_constraints() {
    let catalog: serde_json::Value =
        serde_json::from_str(CATALOG_MANIFEST_JSON).expect("catalog manifest must be valid JSON");
    for column in [
        "plan_review_evidence.static_writer_session_id",
        "plan_review_evidence.static_coordinator_session_id",
        "plan_review_evidence.source_plan_path",
    ] {
        assert!(
            catalog["columns"]
                .as_array()
                .expect("catalog columns")
                .iter()
                .any(|entry| entry["name"] == column),
            "catalog columns missing {column}"
        );
    }
    assert!(
        catalog["constraints"]
            .as_array()
            .expect("catalog constraints")
            .iter()
            .any(
                |entry| entry["name"] == "plan_review_evidence.plan_review_evidence_static_binding"
            )
    );
}

#[test]
fn baseline_resolve_tool_session_returns_checkout_columns() {
    let sql = gobby_core::schema::BASELINE_SQL;
    assert!(sql.contains(
        "RETURNS TABLE(session_id uuid, project_id uuid, machine_id uuid, root_path text)"
    ));
    assert!(sql.contains("LEFT JOIN public.project_checkouts AS checkout"));
    assert!(!sql.contains("SELECT session.id, project.id, project.repo_path"));
}

#[test]
fn catalog_pins_project_checkouts_and_drops_projects_repo_path() {
    let catalog: serde_json::Value =
        serde_json::from_str(CATALOG_MANIFEST_JSON).expect("catalog manifest must be valid JSON");
    let names = |kind: &str| -> Vec<&str> {
        catalog[kind]
            .as_array()
            .unwrap_or_else(|| panic!("{kind} must be an array"))
            .iter()
            .filter_map(|entry| entry["name"].as_str())
            .collect()
    };
    let columns = names("columns");
    for column in [
        "project_checkouts.machine_id",
        "project_checkouts.project_id",
        "project_checkouts.root_path",
        "project_checkouts.created_at",
        "project_checkouts.updated_at",
    ] {
        assert!(
            columns.contains(&column),
            "catalog columns missing {column}"
        );
    }
    assert!(!columns.contains(&"projects.repo_path"));
    let constraints = names("constraints");
    for constraint in [
        "project_checkouts.project_checkouts_pkey",
        "project_checkouts.project_checkouts_machine_id_root_path_key",
        "project_checkouts.project_checkouts_machine_id_fkey",
        "project_checkouts.project_checkouts_project_id_fkey",
    ] {
        assert!(
            constraints.contains(&constraint),
            "catalog constraints missing {constraint}"
        );
    }
}

#[test]
fn code_overlay_pin_schema_contract() {
    let catalog: serde_json::Value =
        serde_json::from_str(CATALOG_MANIFEST_JSON).expect("catalog manifest must be valid JSON");
    let names = |kind: &str| -> Vec<&str> {
        catalog[kind]
            .as_array()
            .unwrap_or_else(|| panic!("{kind} must be an array"))
            .iter()
            .filter_map(|entry| entry["name"].as_str())
            .collect()
    };
    let columns = names("columns");
    for column in [
        "code_overlay_pins.machine_id",
        "code_overlay_pins.overlay_project_id",
        "code_overlay_pins.file_path",
        "code_overlay_pins.source_project_id",
        "code_overlay_pins.content_hash",
        "code_overlay_pins.git_blob_id",
        "code_overlay_pins.graph_projected",
        "code_overlay_pins.vector_projected",
    ] {
        assert!(
            columns.contains(&column),
            "catalog columns missing {column}"
        );
    }
    let constraints = names("constraints");
    for constraint in [
        "code_overlay_pins.code_overlay_pins_pkey",
        "code_overlay_pins.code_overlay_pins_source_content_fkey",
    ] {
        assert!(
            constraints.contains(&constraint),
            "catalog constraints missing {constraint}"
        );
    }
    let source_fk = catalog["constraints"]
        .as_array()
        .expect("catalog constraints")
        .iter()
        .find(|entry| entry["name"] == "code_overlay_pins.code_overlay_pins_source_content_fkey")
        .expect("source content foreign key");
    assert!(
        source_fk["definition"].as_str().is_some_and(|definition| {
            definition
                .contains("REFERENCES code_indexed_files(project_id, file_path, content_hash)")
        }),
        "pin source must reference the immutable content version"
    );
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
