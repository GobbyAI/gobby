use super::super::{IndexDegradation, IndexDurations, IndexOutcome, IndexRequest};
use crate::models::{CallRelation, CallTargetKind};
use serde::Serialize;
use serde::de::DeserializeOwned;
use std::path::PathBuf;

fn assert_cli_independent_contract<T>()
where
    T: Serialize + DeserializeOwned,
{
    let type_name = std::any::type_name::<T>();
    assert!(!type_name.contains("commands::"), "{type_name}");
    assert!(!type_name.contains("output::"), "{type_name}");
    assert!(!type_name.contains("clap"), "{type_name}");
}

#[test]
fn library_api_is_cli_independent() {
    assert_cli_independent_contract::<IndexRequest>();
    assert_cli_independent_contract::<IndexOutcome>();
    assert_cli_independent_contract::<IndexDurations>();
    assert_cli_independent_contract::<IndexDegradation>();

    let request = IndexRequest {
        project_root: PathBuf::from("/tmp/project"),
        path_filter: Some(PathBuf::from("src")),
        explicit_files: vec![PathBuf::from("src/lib.rs")],
        full: true,
        require_cpp_semantics: false,
        sync_projections: true,
    };

    let json = serde_json::to_value(&request).expect("request serializes");
    assert_eq!(json["project_root"], "/tmp/project");
    assert_eq!(json["path_filter"], "src");
    assert_eq!(json["explicit_files"][0], "src/lib.rs");
}

#[test]
fn invalidate_postgres_deletes_are_project_scoped() {
    let source = include_str!("../lifecycle.rs");
    for expected in [
        "DELETE FROM code_symbols WHERE project_id = $1",
        "DELETE FROM code_indexed_files WHERE project_id = $1",
        "DELETE FROM code_content_chunks WHERE project_id = $1",
        "DELETE FROM code_imports WHERE project_id = $1",
        "DELETE FROM code_calls WHERE project_id = $1",
        "DELETE FROM code_indexed_projects WHERE id = $1",
    ] {
        assert!(
            source.contains(expected),
            "missing scoped delete: {expected}"
        );
    }
    let truncate_code = ["TRUNCATE", " code_"].concat();
    let drop_table = ["DROP", " TABLE"].concat();
    assert!(!source.contains(&truncate_code));
    assert!(!source.contains(&drop_table));
}

#[test]
fn call_relation_contract_uses_empty_optional_storage_values() {
    let resolved = CallRelation::new(
        "caller-1".to_string(),
        "foo".to_string(),
        "src/main.py".to_string(),
        12,
    )
    .with_symbol_target("callee-1".to_string());
    let unresolved = CallRelation::new(
        "caller-2".to_string(),
        "bar".to_string(),
        "src/main.py".to_string(),
        18,
    );

    assert_eq!(
        resolved.callee_symbol_id.as_deref().unwrap_or(""),
        "callee-1"
    );
    assert_eq!(unresolved.callee_symbol_id.as_deref().unwrap_or(""), "");
    assert_eq!(resolved.callee_target_kind, CallTargetKind::Symbol);
    assert_eq!(unresolved.callee_target_kind, CallTargetKind::Unresolved);
}
