use super::*;
use crate::config::TEST_ENV_LOCK;
use std::sync::MutexGuard;

struct EnvGuard {
    lock: MutexGuard<'static, ()>,
}

impl EnvGuard {
    fn new() -> Self {
        let guard = Self {
            lock: TEST_ENV_LOCK
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner()),
        };
        guard.clear();
        guard
    }

    fn clear(&self) {
        let _held_env_lock = &self.lock;
        for key in [
            "GOBBY_FALKORDB_HOST",
            "GOBBY_FALKORDB_PORT",
            "GOBBY_FALKORDB_PASSWORD",
            "GOBBY_POSTGRES_DSN",
            "GOBBY_QDRANT_URL",
            "GOBBY_QDRANT_API_KEY",
            "GOBBY_EMBEDDING_URL",
            "GOBBY_EMBEDDING_MODEL",
            "GOBBY_EMBEDDING_API_KEY",
            "GOBBY_EMBEDDING_QUERY_PREFIX",
            "GOBBY_EMBEDDING_TIMEOUT_SECONDS",
        ] {
            // SAFETY: TEST_ENV_LOCK serializes all test environment mutation
            // here, and the loop only touches the fixed key list above.
            unsafe { std::env::remove_var(key) };
        }
    }
}

impl Drop for EnvGuard {
    fn drop(&mut self) {
        self.clear();
    }
}

fn write_services_stack(home: &Path) {
    fs::create_dir_all(services_dir(home)).expect("create services dir");
    fs::write(compose_file_path(home), "services: {}\n").expect("write compose file");
}

#[test]
fn gcore_yaml_reads_flat_and_nested_keys() {
    let config = StandaloneConfig::from_yaml_str(&format!(
        r#"
databases.postgres.dsn: postgresql://flat/db
databases:
  falkordb:
    port: 16379
ai.embeddings:
  provider: ollama
{api_key}: local-api-key
"#,
        api_key = embedding_keys::AI_API_KEY,
    ))
    .expect("parse config");

    assert_eq!(
        config.get("databases.postgres.dsn"),
        Some("postgresql://flat/db")
    );
    assert_eq!(config.get("databases.falkordb.port"), Some("16379"));
    assert_eq!(config.get("ai.embeddings.provider"), Some("ollama"));
    assert_eq!(
        config.get(embedding_keys::AI_API_KEY),
        Some("local-api-key")
    );
}

#[test]
fn gcore_yaml_reads_dotted_mapping_prefixes() {
    let config = StandaloneConfig::from_yaml_str(
        r#"
ai.embeddings:
  provider: ollama
  model: nomic-embed-text
"#,
    )
    .expect("parse config");

    assert_eq!(config.get("ai.embeddings.provider"), Some("ollama"));
    assert_eq!(config.get("ai.embeddings.model"), Some("nomic-embed-text"));
}

#[test]
fn gcore_yaml_writes_nested_keys() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join(GCORE_CONFIG_FILENAME);
    let mut config = StandaloneConfig::empty();
    config.set("databases.postgres.dsn", "postgresql://local/db");
    config.set(embedding_keys::AI_DIM, "768");

    config.write_at(&path).expect("write config");
    let raw = fs::read_to_string(&path).expect("read config");
    let yaml: serde_yaml::Value = serde_yaml::from_str(&raw).expect("parse written yaml");

    assert!(!raw.contains("databases.postgres.dsn:"));
    assert_eq!(
        nested_yaml_str(&yaml, &["databases", "postgres", "dsn"]),
        Some("postgresql://local/db")
    );
    assert_eq!(
        nested_yaml_str(&yaml, &["ai", "embeddings", "dim"]),
        Some("768")
    );
    assert_eq!(
        StandaloneConfig::read_at(&path)
            .expect("read config")
            .expect("config present")
            .get(embedding_keys::AI_DIM),
        Some("768")
    );
}

#[test]
fn gcore_yaml_rejects_excessive_nesting() {
    let mut yaml = String::new();
    for depth in 0..=65 {
        yaml.push_str(&"  ".repeat(depth));
        yaml.push_str(&format!("k{depth}:\n"));
    }
    yaml.push_str(&"  ".repeat(66));
    yaml.push_str("value: too-deep\n");

    let error = StandaloneConfig::from_yaml_str(&yaml).expect_err("too-deep YAML must fail");

    assert!(
        error
            .to_string()
            .contains("gcore.yaml nesting exceeds maximum depth of 64")
    );
}

fn nested_yaml_str<'a>(value: &'a serde_yaml::Value, path: &[&str]) -> Option<&'a str> {
    let mut current = value;
    for key in path {
        current = current
            .as_mapping()?
            .get(&serde_yaml::Value::String((*key).to_string()))?;
    }
    current.as_str()
}

#[test]
fn standalone_config_resolves_service_keys_and_plain_api_key() {
    let _env = EnvGuard::new();
    let mut config = StandaloneConfig::from_yaml_str(&format!(
        r#"
databases.falkordb.host: 127.0.0.1
databases.falkordb.port: "16379"
databases.falkordb.password: falkor-pass
databases.qdrant.url: http://localhost:6333
{api_base}: http://localhost:1234/v1
{model}: text-embedding-nomic-embed-text-v1.5@f16
{api_key}: test-key
"#,
        api_base = embedding_keys::AI_API_BASE,
        model = embedding_keys::AI_MODEL,
        api_key = embedding_keys::AI_API_KEY,
    ))
    .expect("parse config");

    let falkor = crate::config::resolve_falkordb_config(&mut config).expect("falkor");
    assert_eq!(falkor.password.as_deref(), Some("falkor-pass"));
    let qdrant = crate::config::resolve_qdrant_config(&mut config).expect("qdrant");
    assert_eq!(qdrant.url.as_deref(), Some("http://localhost:6333"));
    let embedding = crate::config::resolve_embedding_config(&mut config).expect("embedding");
    assert_eq!(embedding.api_key.as_deref(), Some("test-key"));
}

#[test]
fn writes_ai_embeddings_standalone_api_key() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join(GCORE_CONFIG_FILENAME);
    let options = DockerServiceOptions::new(dir.path().join(".gobby"));
    let embedding = EmbeddingBootstrap {
        provider: "openai-compatible".to_string(),
        api_base: "http://localhost:1234/v1".to_string(),
        model: "embed-small".to_string(),
        vector_dim: 1024,
        query_prefix: Some("query: ".to_string()),
        api_key: Some("local-api-key".to_string()),
    };

    let config = write_standalone_bootstrap(
        &path,
        "postgresql://localhost/gobby",
        &options,
        None,
        Some(&embedding),
    )
    .expect("write standalone bootstrap");

    assert_eq!(
        config.get(embedding_keys::AI_PROVIDER),
        Some("openai-compatible")
    );
    assert_eq!(
        config.get(embedding_keys::AI_API_BASE),
        Some("http://localhost:1234/v1")
    );
    assert_eq!(config.get(embedding_keys::AI_MODEL), Some("embed-small"));
    assert_eq!(config.get(embedding_keys::AI_DIM), Some("1024"));
    assert_eq!(config.get(embedding_keys::AI_QUERY_PREFIX), Some("query: "));
    assert_eq!(
        config.get(embedding_keys::AI_API_KEY),
        Some("local-api-key")
    );
    for key in embedding_keys::legacy_keys() {
        assert_eq!(config.get(&key), None, "legacy key was written: {key}");
    }
}

#[test]
fn compose_template_matches_daemon_checkout_when_present() {
    let Some(daemon) = daemon_compose_template_path() else {
        return;
    };
    if !daemon.exists() {
        return;
    }
    let daemon_template = fs::read_to_string(daemon).expect("read daemon compose template");
    assert_eq!(COMPOSE_TEMPLATE, daemon_template);
}

fn daemon_compose_template_path() -> Option<PathBuf> {
    if let Ok(path) = std::env::var("GOBBY_DAEMON_COMPOSE_PATH") {
        let path = path.trim();
        if !path.is_empty() {
            return Some(PathBuf::from(path));
        }
    }

    let manifest_dir = Path::new(env!("CARGO_MANIFEST_DIR"));
    let repo_root = manifest_dir.parent()?.parent()?;
    Some(
        repo_root
            .parent()?
            .join("gobby/src/gobby/data/docker-compose.services.yml"),
    )
}

#[test]
fn docker_provisioning_prepares_assets_runs_compose_and_health_checks() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut runner = RecordingRunner::default();
    let mut health = RecordingHealth::default();
    let options = DockerServiceOptions::new(dir.path().join(".gobby"));

    let report = provision_docker_services_with(&options, &mut runner, &mut health)
        .expect("provision services");

    assert_eq!(runner.commands.len(), 1);
    assert_eq!(runner.commands[0].program, "docker");
    assert!(runner.commands[0].args.contains(&"--profile".to_string()));
    assert!(runner.commands[0].args.contains(&"all".to_string()));
    assert_eq!(health.checks, vec!["postgres", "qdrant", "falkordb"]);
    assert!(health.endpoints.contains(&(
        "qdrant",
        DEFAULT_QDRANT_HOST.to_string(),
        DEFAULT_QDRANT_HTTP_PORT
    )));
    assert_eq!(report.started_profiles, vec!["all"]);
    assert_eq!(report.health_checks, vec!["postgres", "qdrant", "falkordb"]);
    assert_eq!(
        fs::read_to_string(&report.compose_file).expect("read compose"),
        COMPOSE_TEMPLATE
    );
    assert!(
        report
            .services_dir
            .join("postgres-pgsearch")
            .join("Dockerfile")
            .exists()
    );
    assert!(
        fs::read_to_string(&report.env_file)
            .expect("read env")
            .contains("GOBBY_PG_SEARCH_VERSION=0.23.4")
    );
}

#[test]
fn ensure_hub_reuses_then_provisions() {
    let _env = EnvGuard::new();
    let dir = tempfile::tempdir().expect("tempdir");
    let home = dir.path().join(".gobby");
    fs::create_dir_all(&home).expect("create gobby home");
    write_services_stack(&home);
    let mut config = StandaloneConfig::empty();
    config.set("databases.postgres.dsn", "postgresql://reachable/gobby");
    config
        .write_at(&gcore_config_path(&home))
        .expect("write gcore config");

    let options = EnsureHubOptions::new(home.clone());
    let (database_url, report) = ensure_hub_with(
        &options,
        |_| None,
        |url| url == "postgresql://reachable/gobby",
        |_| panic!("reachable DSN should not provision services"),
    )
    .expect("reuse reachable DSN");
    assert_eq!(database_url, "postgresql://reachable/gobby");
    assert!(report.is_none());

    let mut options = EnsureHubOptions::new(home);
    options.service_options.postgres_port = 15432;
    options.candidate_database_urls = vec!["postgresql://unreachable/gobby".to_string()];
    let (database_url, report) = ensure_hub_with(
        &options,
        |_| None,
        |_| false,
        |service_options| {
            Ok(DockerProvisioningReport {
                services_dir: service_options.gobby_home.join("services"),
                compose_file: compose_file_path(&service_options.gobby_home),
                env_file: service_options.gobby_home.join("services/.env"),
                started_profiles: vec!["all".to_string()],
                health_checks: vec!["postgres".to_string()],
            })
        },
    )
    .expect("provision fallback hub");
    #[cfg(feature = "postgres")]
    {
        assert_eq!(database_url, default_database_url(15432));
        assert_eq!(
            report.expect("provisioning report").health_checks,
            vec!["postgres"]
        );
    }
    #[cfg(not(feature = "postgres"))]
    {
        assert_eq!(database_url, "postgresql://unreachable/gobby");
        assert!(report.is_none());
    }
}

#[test]
fn gcore_yaml_database_url_requires_services_stack() {
    let _env = EnvGuard::new();
    let dir = tempfile::tempdir().expect("tempdir");
    let home = dir.path().join(".gobby");
    fs::create_dir_all(&home).expect("create gobby home");
    let mut config = StandaloneConfig::empty();
    config.set("databases.postgres.dsn", "postgresql://recorded/gobby");
    config
        .write_at(&gcore_config_path(&home))
        .expect("write gcore config");

    let mut options = EnsureHubOptions::new(home);
    options.service_options.postgres_port = 15432;
    let (database_url, report) = ensure_hub_with(
        &options,
        |_| None,
        |url| url == "postgresql://recorded/gobby",
        |service_options| {
            Ok(DockerProvisioningReport {
                services_dir: service_options.gobby_home.join("services"),
                compose_file: compose_file_path(&service_options.gobby_home),
                env_file: service_options.gobby_home.join("services/.env"),
                started_profiles: vec!["all".to_string()],
                health_checks: vec!["postgres".to_string()],
            })
        },
    )
    .expect("missing services stack should provision fallback hub");

    assert_eq!(database_url, default_database_url(15432));
    assert!(report.is_some());
}

#[test]
fn no_double_provision_when_reachable() {
    let _env = EnvGuard::new();
    let dir = tempfile::tempdir().expect("tempdir");
    let home = dir.path().join(".gobby");
    fs::create_dir_all(&home).expect("create gobby home");
    write_services_stack(&home);
    let mut config = StandaloneConfig::empty();
    config.set("databases.postgres.dsn", "postgresql://recorded/gobby");
    config
        .write_at(&gcore_config_path(&home))
        .expect("write gcore config");

    let mut options = EnsureHubOptions::new(home);
    options.service_options.postgres_port = 15432;
    let (database_url, report) = ensure_hub_with_identity(
        &options,
        |_| None,
        |url| url == "postgresql://recorded/gobby",
        |_| {
            Ok(HubIdentityProbeResult::Known(HubIdentity {
                system_identifier: "cluster-a".to_string(),
                database_name: "gobby".to_string(),
            }))
        },
        |_| panic!("reachable recorded DSN should not provision services"),
    )
    .expect("reuse reachable recorded hub");

    assert_eq!(database_url, "postgresql://recorded/gobby");
    assert!(report.is_none());
}

#[test]
fn divergent_hubs_surface_conflict() {
    let _env = EnvGuard::new();
    let dir = tempfile::tempdir().expect("tempdir");
    let home = dir.path().join(".gobby");
    fs::create_dir_all(&home).expect("create gobby home");
    write_services_stack(&home);
    let mut config = StandaloneConfig::empty();
    config.set(
        "databases.postgres.dsn",
        "postgresql://standalone:secret@standalone/gobby?sslmode=require",
    );
    config
        .write_at(&gcore_config_path(&home))
        .expect("write gcore config");
    fs::write(
            home.join("bootstrap.yaml"),
            "hub_backend: postgres\ndatabase_url: postgresql://daemon:secret@daemon/gobby?application_name=gobby\n",
        )
        .expect("write bootstrap");

    let err = ensure_hub_with_identity(
        &EnsureHubOptions::new(home),
        |_| None,
        |url| {
            matches!(
                url,
                "postgresql://standalone:secret@standalone/gobby?sslmode=require"
                    | "postgresql://daemon:secret@daemon/gobby?application_name=gobby"
            )
        },
        |url| {
            let system_identifier = if url.starts_with("postgresql://standalone:secret@standalone/")
            {
                "cluster-a"
            } else {
                "cluster-b"
            };
            Ok(HubIdentityProbeResult::Known(HubIdentity {
                system_identifier: system_identifier.to_string(),
                database_name: "gobby".to_string(),
            }))
        },
        |_| panic!("conflicting reachable hubs should not provision services"),
    )
    .expect_err("surface divergent hub conflict");

    let message = err.to_string();
    assert!(message.contains("system_identifier=cluster-a, database=gobby"));
    assert!(message.contains("system_identifier=cluster-b, database=gobby"));
    assert!(!message.contains("postgresql://"));
    assert!(!message.contains("secret"));
    assert!(!message.contains("sslmode"));
    assert!(!message.contains("application_name"));
}

#[test]
fn reachable_env_database_url_conflicts_with_recorded_hub() {
    let _env = EnvGuard::new();
    let dir = tempfile::tempdir().expect("tempdir");
    let home = dir.path().join(".gobby");
    fs::create_dir_all(&home).expect("create gobby home");
    write_services_stack(&home);
    let mut config = StandaloneConfig::empty();
    config.set("databases.postgres.dsn", "postgresql://recorded/gobby");
    config
        .write_at(&gcore_config_path(&home))
        .expect("write gcore config");

    let err = ensure_hub_with_identity(
        &EnsureHubOptions::new(home),
        |name| (name == "GOBBY_POSTGRES_DSN").then(|| "postgresql://env/gobby".to_string()),
        |url| {
            matches!(
                url,
                "postgresql://recorded/gobby" | "postgresql://env/gobby"
            )
        },
        |url| {
            let system_identifier = if url == "postgresql://recorded/gobby" {
                "cluster-a"
            } else {
                "cluster-b"
            };
            Ok(HubIdentityProbeResult::Known(HubIdentity {
                system_identifier: system_identifier.to_string(),
                database_name: "gobby".to_string(),
            }))
        },
        |_| panic!("conflicting reachable hubs should not provision services"),
    )
    .expect_err("env DSN should be validated against recorded hub");

    let message = err.to_string();
    assert!(message.contains("system_identifier=cluster-a, database=gobby"));
    assert!(message.contains("system_identifier=cluster-b, database=gobby"));
    assert!(!message.contains("postgresql://"));
}

#[test]
fn insufficient_identity_privilege_preserves_hub() {
    let _env = EnvGuard::new();
    let dir = tempfile::tempdir().expect("tempdir");
    let home = dir.path().join(".gobby");
    fs::create_dir_all(&home).expect("create gobby home");
    write_services_stack(&home);
    let mut config = StandaloneConfig::empty();
    config.set("databases.postgres.dsn", "postgresql://standalone/gobby");
    config
        .write_at(&gcore_config_path(&home))
        .expect("write gcore config");
    fs::write(
        home.join("bootstrap.yaml"),
        "hub_backend: postgres\ndatabase_url: postgresql://daemon/gobby\n",
    )
    .expect("write bootstrap");

    let (database_url, report) = ensure_hub_with_identity(
        &EnsureHubOptions::new(home),
        |_| None,
        |url| {
            matches!(
                url,
                "postgresql://standalone/gobby" | "postgresql://daemon/gobby"
            )
        },
        |_| {
            Ok(HubIdentityProbeResult::UnknownInsufficientPrivilege {
                message: "identity_unknown_insufficient_privilege".to_string(),
            })
        },
        |_| panic!("unknown identity for reachable hubs should not provision services"),
    )
    .expect("preserve existing recorded hub");

    assert_eq!(database_url, "postgresql://standalone/gobby");
    assert!(report.is_none());

    let resolution = resolve_recorded_hub_database_url(
        Some("postgresql://standalone:secret@standalone/gobby"),
        Some("postgresql://daemon:secret@daemon/gobby"),
        |url| {
            matches!(
                url,
                "postgresql://standalone:secret@standalone/gobby"
                    | "postgresql://daemon:secret@daemon/gobby"
            )
        },
        |_| {
            Ok(HubIdentityProbeResult::UnknownInsufficientPrivilege {
                message: "identity_unknown_insufficient_privilege".to_string(),
            })
        },
    )
    .expect("resolve unknown identity")
    .expect("resolution");
    let RecordedHubIdentityStatus::IdentityUnknownInsufficientPrivilege { message } =
        resolution.identity_status
    else {
        panic!("expected insufficient privilege status");
    };
    assert!(!message.contains("postgresql://"));
    assert!(!message.contains("secret"));
}

#[test]
fn override_plus_recorded_hub_preserves_recorded_when_identity_unknown() {
    let _env = EnvGuard::new();
    let dir = tempfile::tempdir().expect("tempdir");
    let home = dir.path().join(".gobby");
    fs::create_dir_all(&home).expect("create gobby home");
    write_services_stack(&home);
    let mut config = StandaloneConfig::empty();
    config.set("databases.postgres.dsn", "postgresql://recorded/gobby");
    config
        .write_at(&gcore_config_path(&home))
        .expect("write gcore config");

    let (database_url, report) = ensure_hub_with_identity(
        &EnsureHubOptions::new(home),
        |name| (name == "GOBBY_POSTGRES_DSN").then(|| "postgresql://override/gobby".to_string()),
        |url| {
            matches!(
                url,
                "postgresql://recorded/gobby" | "postgresql://override/gobby"
            )
        },
        |_| {
            Ok(HubIdentityProbeResult::UnknownInsufficientPrivilege {
                message: "identity_unknown_insufficient_privilege".to_string(),
            })
        },
        |_| panic!("reachable hubs with unknown identity should not provision services"),
    )
    .expect("preserve recorded hub when override identity cannot be verified");

    assert_eq!(database_url, "postgresql://recorded/gobby");
    assert!(report.is_none());
}

#[derive(Default)]
struct RecordingRunner {
    commands: Vec<CommandSpec>,
}

impl CommandRunner for RecordingRunner {
    fn run(&mut self, spec: &CommandSpec) -> std::io::Result<CommandOutput> {
        self.commands.push(spec.clone());
        Ok(CommandOutput {
            status: 0,
            stdout: String::new(),
            stderr: String::new(),
        })
    }
}

#[derive(Default)]
struct RecordingHealth {
    checks: Vec<&'static str>,
    endpoints: Vec<(&'static str, String, u16)>,
}

impl DockerHealthChecker for RecordingHealth {
    fn wait_postgres(&mut self, host: &str, port: u16) -> anyhow::Result<()> {
        self.checks.push("postgres");
        self.endpoints.push(("postgres", host.to_string(), port));
        Ok(())
    }

    fn wait_qdrant(&mut self, host: &str, port: u16) -> anyhow::Result<()> {
        self.checks.push("qdrant");
        self.endpoints.push(("qdrant", host.to_string(), port));
        Ok(())
    }

    fn wait_falkordb(&mut self, host: &str, port: u16) -> anyhow::Result<()> {
        self.checks.push("falkordb");
        self.endpoints.push(("falkordb", host.to_string(), port));
        Ok(())
    }
}
