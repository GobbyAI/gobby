use std::io::{Read, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::thread;

use super::context::{resolve_project_by_name, resolve_project_id};
use super::services::{
    resolve_code_vector_settings_from_values, resolve_embedding_config_from_fallible_values,
    resolve_embedding_config_from_values, resolve_falkordb_config_from_values,
    resolve_qdrant_config_from_values,
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

fn write_isolation_json(root: &Path, json: serde_json::Value) {
    let gobby_dir = root.join(".gobby");
    std::fs::create_dir_all(&gobby_dir).expect("create .gobby");
    std::fs::write(
        gobby_dir.join("isolation.json"),
        serde_json::to_string_pretty(&json).expect("serialize isolation json"),
    )
    .expect("write isolation json");
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

const SERVICE_ENV_KEYS: &[&str] = &["GOBBY_INDEXING_RESPECT_GITIGNORE"];

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
fn production_ai_sources_use_effective_config_helpers() {
    let sources = [
        ("search", include_str!("../commands/search.rs")),
        ("symbols", include_str!("../commands/symbols.rs")),
        (
            "code symbol embeddings",
            include_str!("../vector/code_symbols/embedding.rs"),
        ),
    ];

    for (name, source) in sources {
        for forbidden in [
            "AiConfigSource::with_primary(",
            "AiConfigSource::with_primary_from_gobby_home(",
            "LocalAiConfigSource::from_gobby_home(",
        ] {
            assert!(
                !source.contains(forbidden),
                "{name} still constructs an AI source through {forbidden}"
            );
        }
    }
}

#[test]
#[serial_test::serial]
fn adapter_env_precedence_and_json_decode() {
    with_service_env(&[], || {
        let values = std::collections::HashMap::from([
            ("databases.falkordb.host", r#""stored-falkor.local""#),
            ("databases.falkordb.port", r#""16380""#),
            ("databases.falkordb.password", r#""stored-pass""#),
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
        });

        assert_eq!(falkor.host, "stored-falkor.local");
        assert_eq!(falkor.port, 16380);
        assert_eq!(falkor.password.as_deref(), Some("stored-pass"));
        assert_eq!(falkor.graph_name, FALKORDB_GRAPH_NAME);
        assert_eq!(qdrant.url.as_deref(), Some("http://qdrant.local:6333"));
        assert_eq!(qdrant.api_key.as_deref(), Some("qdrant-key"));
        let embedding = embedding.expect("daemon-served embedding config");
        assert_eq!(embedding.api_base, "http://embeddings.local:11434");
        assert_eq!(embedding.model, "embed-model");
    });
}

fn serve_json_once(body: serde_json::Value) -> (String, thread::JoinHandle<String>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test daemon");
    let daemon_url = format!(
        "http://127.0.0.1:{}",
        listener.local_addr().expect("test daemon address").port()
    );
    let handle = thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("accept test daemon request");
        let mut request = [0_u8; 4096];
        let size = stream.read(&mut request).expect("read test daemon request");
        let request_line = String::from_utf8_lossy(&request[..size])
            .lines()
            .next()
            .unwrap_or_default()
            .to_string();
        let body = serde_json::to_vec(&body).expect("serialize test daemon response");
        write!(
            stream,
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
            body.len()
        )
        .expect("write test daemon response headers");
        stream
            .write_all(&body)
            .expect("write test daemon response body");
        request_line
    });
    (daemon_url, handle)
}

#[test]
#[serial_test::serial(serial_db)]
fn project_name_lookup_uses_the_calling_machine_checkout() {
    let (daemon_url, request) = serve_json_once(serde_json::json!([{
        "id": "00000000-0000-4000-8000-000000000001",
        "name": "gobby",
        "repo_path": "/legacy/repo-path",
        "root_path": "/foreign/index-root",
        "checkout": {
            "machine_id": "00000000-0000-4000-8000-000000000010",
            "root_path": "/local/checkout"
        }
    }]));

    temp_env::with_var("GOBBY_DAEMON_URL", Some(&daemon_url), || {
        let root = resolve_project_by_name("gobby").expect("local checkout");
        assert_eq!(root, PathBuf::from("/local/checkout"));
    });
    assert_eq!(
        request.join().expect("test daemon thread"),
        "GET /api/projects HTTP/1.1"
    );
}

#[test]
#[serial_test::serial(serial_db)]
fn project_name_lookup_rejects_another_machines_index_root() {
    let (daemon_url, request) = serve_json_once(serde_json::json!([{
        "id": "00000000-0000-4000-8000-000000000001",
        "name": "gobby",
        "root_path": "/foreign/index-root",
        "checkout": null
    }]));

    temp_env::with_var("GOBBY_DAEMON_URL", Some(&daemon_url), || {
        let error = resolve_project_by_name("gobby")
            .expect_err("foreign index root must not resolve locally");
        assert_eq!(
            error
                .downcast_ref::<crate::cli_error::CliError>()
                .map(|error| error.code),
            Some("project_not_found")
        );
    });
    assert_eq!(
        request.join().expect("test daemon thread"),
        "GET /api/projects HTTP/1.1"
    );
}

#[test]
#[serial_test::serial(serial_db)]
fn project_name_lookup_prefers_active_project_over_deleted_duplicate() {
    let (daemon_url, request) = serve_json_once(serde_json::json!([
        {
            "id": "00000000-0000-4000-8000-000000000001",
            "name": "gobby",
            "deleted_at": "2026-08-30T12:00:00Z",
            "checkout": {
                "machine_id": "00000000-0000-4000-8000-000000000010",
                "root_path": "/local/deleted-checkout"
            }
        },
        {
            "id": "00000000-0000-4000-8000-000000000002",
            "name": "gobby",
            "deleted_at": null,
            "checkout": {
                "machine_id": "00000000-0000-4000-8000-000000000010",
                "root_path": "/local/active-checkout"
            }
        }
    ]));

    temp_env::with_var("GOBBY_DAEMON_URL", Some(&daemon_url), || {
        let root = resolve_project_by_name("gobby").expect("active checkout");
        assert_eq!(root, PathBuf::from("/local/active-checkout"));
    });
    assert_eq!(
        request.join().expect("test daemon thread"),
        "GET /api/projects HTTP/1.1"
    );
}

#[test]
#[serial_test::serial(serial_db)]
fn project_name_lookup_rejects_deleted_only_name() {
    let (daemon_url, request) = serve_json_once(serde_json::json!([{
        "id": "00000000-0000-4000-8000-000000000001",
        "name": "gobby",
        "deleted_at": "2026-08-30T12:00:00Z",
        "checkout": {
            "machine_id": "00000000-0000-4000-8000-000000000010",
            "root_path": "/local/deleted-checkout"
        }
    }]));

    temp_env::with_var("GOBBY_DAEMON_URL", Some(&daemon_url), || {
        let error =
            resolve_project_by_name("gobby").expect_err("deleted-only name must not resolve");
        assert_eq!(
            error
                .downcast_ref::<crate::cli_error::CliError>()
                .map(|error| error.code),
            Some("project_not_found")
        );
    });
    assert_eq!(
        request.join().expect("test daemon thread"),
        "GET /api/projects HTTP/1.1"
    );
}

#[test]
#[serial_test::serial(serial_db)]
fn daemon_url_falls_back_when_bootstrap_path_is_unavailable() {
    temp_env::with_vars(
        [
            ("GOBBY_DAEMON_URL", None::<&str>),
            ("GOBBY_PORT", None::<&str>),
            ("GOBBY_HOME", Some("/dev/null/not-a-directory")),
        ],
        || {
            assert_eq!(
                gobby_core::daemon_url::daemon_url(),
                "http://127.0.0.1:60887"
            );
        },
    );
}

#[test]
#[serial_test::serial(serial_db)]
fn daemon_url_normalizes_wildcard_bootstrap_bind_host() {
    let temp = tempfile::tempdir().expect("tempdir");
    std::fs::write(
        temp.path().join("bootstrap.yaml"),
        "daemon_port: 61234\nbind_host: 0.0.0.0\n",
    )
    .expect("write bootstrap");

    temp_env::with_vars(
        [
            ("GOBBY_DAEMON_URL", None::<&str>),
            ("GOBBY_PORT", None::<&str>),
            ("GOBBY_HOME", Some(temp.path().to_str().expect("utf8 path"))),
        ],
        || {
            assert_eq!(
                gobby_core::daemon_url::daemon_url(),
                "http://127.0.0.1:61234"
            );
        },
    );
}

#[test]
#[serial_test::serial]
fn adapter_resolves_grant_backed_plaintext() {
    with_service_env(&[], || {
        let values = std::collections::HashMap::from([
            ("databases.falkordb.host", "falkor.local"),
            ("databases.falkordb.password", "grant-falkor"),
            ("databases.qdrant.url", "http://qdrant.local:6333"),
            ("databases.qdrant.api_key", "grant-qdrant"),
            (embedding_keys::AI_API_BASE, "http://embeddings.local:11434"),
            (embedding_keys::AI_API_KEY, "grant-embedding"),
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
        });

        assert_eq!(falkor.password.as_deref(), Some("grant-falkor"));
        assert_eq!(qdrant.api_key.as_deref(), Some("grant-qdrant"));
        let embedding = embedding.expect("grant-backed embedding config");
        assert_eq!(embedding.api_base, "http://embeddings.local:11434");
        assert_eq!(embedding.api_key.as_deref(), Some("grant-embedding"));
    });
}

#[test]
fn embedding_config_source_errors_are_propagated() {
    let error = resolve_embedding_config_from_fallible_values(
        |_key| Err(anyhow::anyhow!("database read failed")),
        |value| Ok(value.to_string()),
    )
    .expect_err("config read failure must not resolve as missing embeddings");

    let message = format!("{error:#}");
    assert!(message.contains("failed to read config key"));
    assert!(message.contains("database read failed"));
}

#[test]
#[serial_test::serial]
fn vector_dim_setting_reads_ai_config_no_env() {
    with_service_env(&[], || {
        let values = std::collections::HashMap::from([(embedding_keys::AI_DIM, "2048")]);

        let settings = resolve_code_vector_settings_from_values(config_value_for(&values))
            .expect("config-store vector settings");
        assert_eq!(settings.vector_dim, Some(2048));

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
            ("databases.falkordb.password", r#""stored-pass""#),
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
#[serial_test::serial]
fn falkor_password_reads_password_key() {
    with_service_env(&[], || {
        let values = std::collections::HashMap::from([
            ("databases.falkordb.host", r#""stored-falkor.local""#),
            ("databases.falkordb.password", r#""stored-pass""#),
        ]);

        let falkor = resolve_falkordb_config_from_values(config_value_for(&values), |value| {
            Ok(value.to_string())
        })
        .expect("falkordb config");

        assert_eq!(falkor.password.as_deref(), Some("stored-pass"));
    });
}

#[test]
#[serial_test::serial]
fn invalid_service_port_warns_and_uses_default() {
    with_service_env(&[], || {
        for raw_port in [r#""0""#, r#""not-a-port""#] {
            let values = std::collections::HashMap::from([
                ("databases.falkordb.host", r#""stored-falkor.local""#),
                ("databases.falkordb.port", raw_port),
            ]);

            let falkor = resolve_falkordb_config_from_values(config_value_for(&values), |value| {
                Ok(value.to_string())
            })
            .expect("falkordb config");

            assert_eq!(falkor.port, 16379);
        }
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
    let project_id = "00000000-0000-4000-8000-000000000042";
    write_project_json(
        tmp.path(),
        serde_json::json!({
            "id": project_id,
            "name": "main"
        }),
    );

    let identity = resolve_project_identity(tmp.path(), MissingIdentity::Error).expect("identity");

    assert_eq!(identity.project_id, project_id);
    assert_eq!(
        resolve_project_id(tmp.path()).expect("marker id"),
        project_id
    );
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
            "name": "main"
        }),
    );
    write_isolation_json(
        &root,
        serde_json::json!({
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
    let parent_project_id = "0f1f5df6-7f37-4a7f-9115-5b473f22934e";
    let parent = tmp.path().join("parent");
    std::fs::create_dir(&parent).expect("create parent");
    let worktree = tmp.path().join("worktree");
    std::fs::create_dir(&worktree).expect("create worktree");
    write_project_json(
        &worktree,
        serde_json::json!({
            "id": "parent-id"
        }),
    );
    write_isolation_json(
        &worktree,
        serde_json::json!({
            "parent_project_path": parent.to_string_lossy(),
            "parent_project_id": parent_project_id
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
            parent_project_id: parent_project_id.to_string(),
            parent_root: parent.canonicalize().unwrap(),
        }
    );
    assert!(!identity.should_write_gcode_json);
    assert!(identity.warning.is_none());
}

#[test]
fn isolated_marker_without_complete_parent_metadata_is_rejected() {
    let tmp = tempfile::tempdir().expect("tempdir");
    write_project_json(
        tmp.path(),
        serde_json::json!({
            "id": "parent-id"
        }),
    );
    write_isolation_json(
        tmp.path(),
        serde_json::json!({
            "parent_project_path": "/parent"
        }),
    );

    let err = resolve_project_identity(tmp.path(), MissingIdentity::Error)
        .expect_err("incomplete parent metadata should fail");

    let message = err.to_string();
    assert!(message.contains("invalid isolation marker in"), "{message}");
    assert!(message.contains(".gobby/isolation.json"), "{message}");
    assert!(
        message.contains("parent_project_path and parent_project_id must be set together"),
        "{message}"
    );
}

#[test]
fn isolated_marker_rejects_missing_parent_path() {
    let tmp = tempfile::tempdir().expect("tempdir");
    write_project_json(
        tmp.path(),
        serde_json::json!({
            "id": "parent-id"
        }),
    );
    write_isolation_json(
        tmp.path(),
        serde_json::json!({
            "parent_project_id": "0f1f5df6-7f37-4a7f-9115-5b473f22934e"
        }),
    );

    let err = resolve_project_identity(tmp.path(), MissingIdentity::Error)
        .expect_err("incomplete parent metadata should fail");

    assert!(err.to_string().contains("must be set together"));
}

#[test]
fn linked_worktree_uses_path_id_and_ignores_copied_project_id() {
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
    assert!(copied.warning.is_none());
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
    let err = match Context::resolve_for_project_id_with_services(
        "  ",
        true,
        ServiceConfigSelection::falkordb_only(),
    ) {
        Ok(_) => panic!("empty project id should fail before DB resolution"),
        Err(err) => err,
    };

    assert!(err.to_string().contains("--project-id must not be empty"));
}

#[test]
fn project_id_projection_cleanup_selection_includes_qdrant_without_embeddings() {
    let services = ServiceConfigSelection::projection_cleanup();

    assert!(services.falkordb);
    assert!(services.qdrant);
    assert!(!services.embedding);
    assert!(!services.code_vectors);
}

#[test]
fn project_id_context_with_services_rejects_empty_id_before_runtime_resolution() {
    let err = match Context::resolve_for_project_id_with_services(
        "  ",
        true,
        ServiceConfigSelection::projection_cleanup(),
    ) {
        Ok(_) => panic!("empty project id should fail before DB resolution"),
        Err(err) => err,
    };

    assert!(err.to_string().contains("--project-id must not be empty"));
}

#[test]
fn identity_for_cwd_preserves_isolation_errors() {
    let tmp = tempfile::tempdir().expect("tempdir");
    write_project_json(
        tmp.path(),
        serde_json::json!({
            "id": "parent-id"
        }),
    );
    write_isolation_json(
        tmp.path(),
        serde_json::json!({
            "parent_project_path": "/parent"
        }),
    );
    let err = super::context::identity_for_resolved_root(tmp.path(), false)
        .expect_err("invalid isolation marker");
    let message = err.to_string();
    assert!(message.contains("invalid isolation marker"), "{message}");
    assert!(
        err.downcast_ref::<crate::cli_error::CliError>().is_none(),
        "specific identity errors must not collapse to project_required"
    );
}

#[test]
fn identity_for_cwd_maps_missing_project_to_project_required() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let err =
        super::context::identity_for_resolved_root(tmp.path(), false).expect_err("missing project");
    let cli = err
        .downcast_ref::<crate::cli_error::CliError>()
        .expect("project_required");
    assert_eq!(cli.code, "project_required");
    assert!(
        err.source()
            .is_some_and(|source| source.to_string().starts_with("No gcode project found")),
        "{err:?}"
    );
}

#[test]
fn grant_settings_supply_indexing_and_vector_dim() {
    with_service_env(&[], || {
        let settings = std::collections::BTreeMap::from([
            (
                gobby_core::config::INDEXING_RESPECT_GITIGNORE_KEY.to_string(),
                "false".to_string(),
            ),
            (embedding_keys::AI_DIM.to_string(), "768".to_string()),
        ]);
        let (embedding, indexing, code_vectors) =
            super::services::resolve_from_grant_settings(&settings, ServiceConfigSelection::all())
                .expect("grant settings");
        assert!(embedding.is_none());
        assert!(!indexing.respect_gitignore);
        assert_eq!(code_vectors.vector_dim, Some(768));
    });
}

mod runtime_contract;
