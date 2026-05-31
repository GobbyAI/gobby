use std::path::{Path, PathBuf};
use std::process::Command;

use super::context::escape_like;
use super::context::resolve_project_id;
use super::services::{
    resolve_code_vector_settings_from_values, resolve_embedding_config_from_values,
    resolve_falkordb_config_from_values, resolve_qdrant_config_from_values,
};
use super::*;
use gobby_core::config::embedding_keys;

fn write_project_json(root: &Path, json: serde_json::Value) {
    let gobby_dir = root.join(".gobby");
    std::fs::create_dir_all(&gobby_dir).expect("create .gobby");
    std::fs::write(
        gobby_dir.join("project.json"),
        serde_json::to_string_pretty(&json).expect("serialize project json"),
    )
    .expect("write project json");
}

fn run_git(dir: &Path, args: &[&str]) {
    let output = Command::new("git")
        .arg("-C")
        .arg(dir)
        .args(args)
        .output()
        .expect("run git");
    assert!(
        output.status.success(),
        "git {:?} failed\nstdout:\n{}\nstderr:\n{}",
        args,
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}

fn create_linked_worktree(tmp: &tempfile::TempDir) -> (PathBuf, PathBuf) {
    let repo = tmp.path().join("repo");
    let linked = tmp.path().join("linked");
    std::fs::create_dir(&repo).expect("create repo");
    run_git(&repo, &["init"]);
    std::fs::write(repo.join("README.md"), "hello\n").expect("write readme");
    run_git(&repo, &["add", "README.md"]);
    run_git(
        &repo,
        &[
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test User",
            "commit",
            "-m",
            "initial",
        ],
    );
    run_git(
        &repo,
        &[
            "worktree",
            "add",
            "-b",
            "linked-branch",
            linked.to_str().unwrap(),
        ],
    );
    (repo, linked)
}

const SERVICE_ENV_KEYS: &[&str] = &[
    "GOBBY_FALKORDB_HOST",
    "GOBBY_FALKORDB_PORT",
    "GOBBY_FALKORDB_PASSWORD",
    "GOBBY_QDRANT_URL",
    "GOBBY_QDRANT_API_KEY",
    "GOBBY_EMBEDDING_URL",
    "GOBBY_EMBEDDING_MODEL",
    "GOBBY_EMBEDDING_API_KEY",
    "GOBBY_EMBEDDING_TIMEOUT_SECONDS",
    "GOBBY_EMBEDDING_VECTOR_DIM",
];

fn with_service_env<R>(
    overrides: &[(&'static str, Option<&'static str>)],
    closure: impl FnOnce() -> R,
) -> R {
    let mut vars = SERVICE_ENV_KEYS
        .iter()
        .map(|key| (*key, None))
        .collect::<Vec<_>>();
    vars.extend_from_slice(overrides);
    temp_env::with_vars(vars, closure)
}

fn config_value_for<'a>(
    values: &'a std::collections::HashMap<&'a str, &'a str>,
) -> impl FnMut(&str) -> Option<String> + 'a {
    |key| values.get(key).map(|value| (*value).to_string())
}

#[test]
#[serial_test::serial]
fn adapter_env_precedence_and_json_decode() {
    with_service_env(&[("GOBBY_FALKORDB_HOST", Some("env-falkor.local"))], || {
        let values = std::collections::HashMap::from([
            ("databases.falkordb.host", r#""stored-falkor.local""#),
            ("databases.falkordb.port", r#""16380""#),
            ("databases.falkordb.requirepass", r#""stored-pass""#),
            ("databases.qdrant.url", r#""http://qdrant.local:6333""#),
            ("databases.qdrant.api_key", r#""qdrant-key""#),
            (
                embedding_keys::AI_API_BASE,
                r#""http://embeddings.local:11434""#,
            ),
            (embedding_keys::AI_MODEL, r#""embed-model""#),
            (embedding_keys::AI_API_KEY, "null"),
        ]);

        let falkor = resolve_falkordb_config_from_values(config_value_for(&values), |value| {
            Ok(value.to_string())
        })
        .expect("falkordb config");
        let qdrant = resolve_qdrant_config_from_values(config_value_for(&values), |value| {
            Ok(value.to_string())
        })
        .expect("qdrant config");
        let embedding = resolve_embedding_config_from_values(config_value_for(&values), |value| {
            Ok(value.to_string())
        })
        .expect("embedding config");

        assert_eq!(falkor.host, "env-falkor.local");
        assert_eq!(falkor.port, 16380);
        assert_eq!(falkor.password.as_deref(), Some("stored-pass"));
        assert_eq!(falkor.graph_name, FALKORDB_GRAPH_NAME);
        assert_eq!(qdrant.url.as_deref(), Some("http://qdrant.local:6333"));
        assert_eq!(qdrant.api_key.as_deref(), Some("qdrant-key"));
        assert_eq!(embedding.api_base, "http://embeddings.local:11434");
        assert_eq!(embedding.model, "embed-model");
        assert_eq!(embedding.api_key, None);
        assert_eq!(embedding.timeout_seconds, 10);
    });
}

#[test]
fn project_name_like_lookup_escapes_wildcards() {
    assert_eq!(escape_like(r"api\_%"), r"api\\\_\%");
}

#[test]
#[serial_test::serial]
fn adapter_resolves_config_store_secrets() {
    with_service_env(&[], || {
        let values = std::collections::HashMap::from([
            ("databases.falkordb.host", "falkor.local"),
            (
                "databases.falkordb.requirepass",
                "$secret:falkordb_password",
            ),
            ("databases.qdrant.url", "http://qdrant.local:6333"),
            ("databases.qdrant.api_key", "$secret:qdrant_api_key"),
            (embedding_keys::AI_API_BASE, "http://embeddings.local:11434"),
            (embedding_keys::AI_API_KEY, "$secret:embedding_api_key"),
        ]);

        fn resolve_secret_stub(value: &str) -> anyhow::Result<String> {
            match value {
                "$secret:falkordb_password" => Ok("resolved-falkor".to_string()),
                "$secret:qdrant_api_key" => Ok("resolved-qdrant".to_string()),
                "$secret:embedding_api_key" => Ok("resolved-embedding".to_string()),
                value => Ok(value.to_string()),
            }
        }

        let falkor =
            resolve_falkordb_config_from_values(config_value_for(&values), resolve_secret_stub)
                .expect("falkordb config");
        let qdrant =
            resolve_qdrant_config_from_values(config_value_for(&values), resolve_secret_stub)
                .expect("qdrant config");
        let embedding =
            resolve_embedding_config_from_values(config_value_for(&values), resolve_secret_stub)
                .expect("embedding config");

        assert_eq!(falkor.password.as_deref(), Some("resolved-falkor"));
        assert_eq!(qdrant.api_key.as_deref(), Some("resolved-qdrant"));
        assert_eq!(embedding.api_key.as_deref(), Some("resolved-embedding"));
    });
}

#[test]
#[serial_test::serial]
fn vector_dim_setting_reads_ai_config_no_env() {
    with_service_env(&[], || {
        let legacy_keys = embedding_keys::legacy_keys();
        let values = std::collections::HashMap::from([(embedding_keys::AI_DIM, "2048")]);

        let settings = resolve_code_vector_settings_from_values(config_value_for(&values))
            .expect("config-store vector settings");
        assert_eq!(settings.vector_dim, Some(2048));

        temp_env::with_var("GOBBY_EMBEDDING_VECTOR_DIM", Some("3072"), || {
            let settings = resolve_code_vector_settings_from_values(config_value_for(&values))
                .expect("env must not override vector settings");
            assert_eq!(settings.vector_dim, Some(2048));
        });

        let legacy_vector_dim_key = legacy_keys
            .iter()
            .find(|key| key.as_str() == "embeddings.vector_dim")
            .expect("legacy embeddings.vector_dim key should be present");
        let legacy_values =
            std::collections::HashMap::from([(legacy_vector_dim_key.as_str(), "1536")]);
        let settings = resolve_code_vector_settings_from_values(config_value_for(&legacy_values))
            .expect("legacy vector dim ignored");
        assert_eq!(settings.vector_dim, None);

        let null_values = std::collections::HashMap::from([(embedding_keys::AI_DIM, "null")]);
        let settings = resolve_code_vector_settings_from_values(config_value_for(&null_values))
            .expect("null config-store vector settings");
        assert_eq!(settings.vector_dim, None);

        let invalid_values =
            std::collections::HashMap::from([(embedding_keys::AI_DIM, r#""wide""#)]);
        let err = resolve_code_vector_settings_from_values(config_value_for(&invalid_values))
            .expect_err("invalid vector dim must error");
        assert!(matches!(
            err,
            CodeVectorConfigError::InvalidVectorDim { .. }
        ));
    });
}

#[test]
#[serial_test::serial]
fn phase7_config_resolution_returns_gcode_falkor_config_with_core_fields_and_graph_name() {
    with_service_env(&[], || {
        let values = std::collections::HashMap::from([
            ("databases.falkordb.host", r#""stored-falkor.local""#),
            ("databases.falkordb.port", r#""16380""#),
            ("databases.falkordb.requirepass", r#""stored-pass""#),
        ]);

        let falkor = resolve_falkordb_config_from_values(config_value_for(&values), |value| {
            Ok(value.to_string())
        })
        .expect("falkordb config");

        assert_eq!(falkor.host, "stored-falkor.local");
        assert_eq!(falkor.port, 16380);
        assert_eq!(falkor.password.as_deref(), Some("stored-pass"));
        assert_eq!(falkor.graph_name, "gobby_code");

        let connection = falkor.connection_config();
        assert_eq!(connection.host, falkor.host);
        assert_eq!(connection.port, falkor.port);
        assert_eq!(connection.password, falkor.password);
    });
}

#[test]
fn test_resolve_project_id_requires_project_context() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let err = resolve_project_id(tmp.path()).expect_err("missing project context must fail");

    assert!(
        err.to_string().contains("No gcode project found"),
        "unexpected error: {err}"
    );
    assert!(
        err.to_string().contains("gcode init"),
        "unexpected error: {err}"
    );
}

#[test]
fn main_repo_keeps_project_json_id() {
    let tmp = tempfile::tempdir().expect("tempdir");
    write_project_json(
        tmp.path(),
        serde_json::json!({
            "id": "main-project-id",
            "name": "main"
        }),
    );

    let identity = resolve_project_identity(tmp.path(), MissingIdentity::Error).expect("identity");

    assert_eq!(identity.project_id, "main-project-id");
    assert_eq!(identity.source, ProjectIdentitySource::ProjectJson);
    assert!(!identity.should_write_gcode_json);
    assert!(identity.warning.is_none());
}

#[test]
fn self_referential_parent_marker_keeps_project_json_id() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path().canonicalize().expect("canonical root");
    write_project_json(
        &root,
        serde_json::json!({
            "id": "main-project-id",
            "name": "main",
            "parent_project_path": root.to_string_lossy(),
            "parent_project_id": "main-project-id"
        }),
    );

    let identity = resolve_project_identity(&root, MissingIdentity::Error).expect("identity");

    assert_eq!(identity.project_id, "main-project-id");
    assert_eq!(identity.source, ProjectIdentitySource::ProjectJson);
    assert!(!identity.should_write_gcode_json);
    assert!(identity.warning.is_none());
}

#[test]
fn isolated_marker_with_parent_metadata_resolves_overlay_scope() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let parent = tmp.path().join("parent");
    std::fs::create_dir(&parent).expect("create parent");
    let worktree = tmp.path().join("worktree");
    std::fs::create_dir(&worktree).expect("create worktree");
    write_project_json(
        &worktree,
        serde_json::json!({
            "id": "parent-id",
            "parent_project_path": parent.to_string_lossy(),
            "parent_project_id": "parent-id"
        }),
    );

    let identity = resolve_project_identity(&worktree, MissingIdentity::Error).expect("identity");

    assert_eq!(
        identity.project_id,
        crate::project::code_index_id_for_root(&worktree)
    );
    assert_eq!(identity.source, ProjectIdentitySource::IsolatedOverlay);
    assert_eq!(
        identity.index_scope,
        ProjectIndexScope::Overlay {
            overlay_project_id: crate::project::code_index_id_for_root(&worktree),
            overlay_root: worktree.canonicalize().unwrap(),
            parent_project_id: "parent-id".to_string(),
            parent_root: parent.canonicalize().unwrap(),
        }
    );
    assert!(!identity.should_write_gcode_json);
    assert!(identity.warning.is_none());
}

#[test]
fn isolated_marker_without_complete_parent_metadata_keeps_single_scope() {
    let tmp = tempfile::tempdir().expect("tempdir");
    write_project_json(
        tmp.path(),
        serde_json::json!({
            "id": "parent-id",
            "parent_project_path": "/parent"
        }),
    );

    let identity = resolve_project_identity(tmp.path(), MissingIdentity::Error).expect("identity");

    assert_eq!(
        identity.project_id,
        crate::project::code_index_id_for_root(tmp.path())
    );
    assert_eq!(identity.source, ProjectIdentitySource::IsolatedRoot);
    assert_eq!(identity.index_scope, ProjectIndexScope::Single);
}

#[test]
fn linked_worktree_uses_path_id_and_warns_only_for_copied_project_id() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let (_repo, linked) = create_linked_worktree(&tmp);

    let identity = resolve_project_identity(&linked, MissingIdentity::Error).expect("identity");

    assert_eq!(
        identity.project_id,
        crate::project::code_index_id_for_root(&linked)
    );
    assert_eq!(identity.source, ProjectIdentitySource::LinkedWorktree);
    assert!(identity.warning.is_none());
    assert!(!identity.should_write_gcode_json);

    write_project_json(
        &linked,
        serde_json::json!({
            "id": "copied-parent-id",
            "name": "linked"
        }),
    );
    let copied =
        resolve_project_identity(&linked, MissingIdentity::Error).expect("copied identity");

    assert_eq!(copied.source, ProjectIdentitySource::LinkedWorktree);
    assert_eq!(
        copied.project_id,
        crate::project::code_index_id_for_root(&linked)
    );
    assert!(copied.warning.as_deref().unwrap_or("").contains("copied"));
    assert!(!copied.should_write_gcode_json);
}

#[test]
fn generated_identity_writes_only_for_non_isolated_roots() {
    let tmp = tempfile::tempdir().expect("tempdir");

    let identity =
        resolve_project_identity(tmp.path(), MissingIdentity::Generate).expect("identity");

    assert_eq!(identity.source, ProjectIdentitySource::Generated);
    assert!(identity.should_write_gcode_json);
    assert_eq!(
        identity.project_id,
        crate::project::code_index_id_for_root(tmp.path())
    );
}

#[test]
fn project_id_only_context_rejects_empty_id_before_runtime_resolution() {
    let err = match Context::resolve_for_project_id("  ", true) {
        Ok(_) => panic!("empty project id should fail before DB resolution"),
        Err(err) => err,
    };

    assert!(err.to_string().contains("--project-id must not be empty"));
}
