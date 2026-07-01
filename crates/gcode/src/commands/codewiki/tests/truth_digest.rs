use std::collections::BTreeMap;

use super::*;

fn model_with_services(services: Vec<ServiceBoundary>) -> SystemModel {
    SystemModel {
        crates: Vec::new(),
        edges: Vec::new(),
        services,
        runtime_modes: Vec::new(),
        features_by_crate: BTreeMap::new(),
        notes: Vec::new(),
    }
}

#[test]
fn truth_digest_renders_slugged_bounded_authoritative_stack() {
    let model = model_with_services(vec![
        ServiceBoundary {
            name: "PostgreSQL hub".to_string(),
            kind: ServiceKind::Postgres,
            pulled_in_by: vec!["gobby-code (feature: postgres)".to_string()],
        },
        ServiceBoundary {
            name: "FalkorDB graph".to_string(),
            kind: ServiceKind::Falkor,
            pulled_in_by: vec!["gobby-code (feature: falkor)".to_string()],
        },
    ]);

    let digest = build_truth_digest(&model, "project-1", 7, 2);

    assert_eq!(digest.schema_version, 1);
    assert_eq!(digest.project_id, "project-1");
    assert_eq!(digest.stack_authority, "complete_current_set");
    assert!(digest.repo_summary.contains("7 files"));
    assert!(digest.repo_summary.contains("2 modules"));
    assert_eq!(digest.stack.len(), 2);
    let postgres = digest
        .stack
        .iter()
        .find(|entry| entry.service == "PostgreSQL hub")
        .expect("postgres stack entry");
    assert_eq!(postgres.kind, "postgres");
    assert_eq!(
        digest.key_paths.get("PostgreSQL hub").map(String::as_str),
        Some("crates/gcore/src/postgres.rs:16")
    );
    assert!(postgres.summary.chars().count() <= 56);
    assert!(postgres.degradation.chars().count() <= 56);

    let json = serde_json::to_string(&digest).expect("serialize digest");
    assert!(!json.contains("Postgres"));
    assert!(!json.contains("superseded"));
    assert!(
        json.len() < 2_500,
        "digest should stay bounded, got {}",
        json.len()
    );
}

#[test]
fn truth_digest_marks_empty_stack_partial() {
    let digest = build_truth_digest(&model_with_services(Vec::new()), "project-1", 0, 0);

    assert_eq!(digest.stack_authority, "partial");
    assert!(digest.stack.is_empty());
    assert!(digest.key_paths.is_empty());
}

#[test]
fn truth_digest_write_is_unscoped_only() {
    let digest = build_truth_digest(&model_with_services(Vec::new()), "project-1", 0, 0);
    let unscoped_dir = tempfile::tempdir().expect("unscoped tempdir");

    write_truth_digest(unscoped_dir.path(), &DocPruneScope::unscoped(), &digest)
        .expect("write unscoped digest");

    let unscoped_path = unscoped_dir.path().join(TRUTH_DIGEST_META_PATH);
    let raw = std::fs::read_to_string(&unscoped_path).expect("read digest");
    let parsed: CodewikiTruthDigest = serde_json::from_str(&raw).expect("parse digest");
    assert_eq!(parsed.schema_version, 1);

    let scoped_dir = tempfile::tempdir().expect("scoped tempdir");
    let scoped = DocPruneScope::from_scopes(&["src/lib.rs".to_string()]);
    write_truth_digest(scoped_dir.path(), &scoped, &digest).expect("skip scoped digest");
    assert!(!scoped_dir.path().join(TRUTH_DIGEST_META_PATH).exists());
}
