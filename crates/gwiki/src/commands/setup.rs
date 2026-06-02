use std::path::{Path, PathBuf};

use gobby_core::config::embedding_keys;
use gobby_core::provisioning::{
    DockerProvisioningReport, DockerServiceOptions, EnsureHubOptions, StandaloneConfig,
    compose_file_path, ensure_hub, gcore_config_path,
};
use gobby_core::setup::{SetupContext, StandaloneSetup, ValidationContext};
use serde_json::json;

use crate::support::env::database_url_from_env;
use crate::support::scope::{resolve_command_scope, resolved_scope_identity};
use crate::support::text::postgres_object_kind;
use crate::{
    CommandOutcome, ScopeIdentity, ScopeSelection, SetupOptions, WikiError, setup as wiki_setup,
};

type PostgresSetupResult = (Vec<String>, Vec<String>, Vec<(String, String)>);

pub(crate) fn execute(
    selection: ScopeSelection,
    options: SetupOptions,
) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    let output_scope = resolved_scope_identity(&scope);
    let setup = wiki_setup::default_setup();
    let objects = setup.postgres_objects().map_err(WikiError::from)?;
    let object_payloads = objects
        .iter()
        .map(|object| {
            json!({
                "name": object.name,
                "kind": postgres_object_kind(object.kind),
                "store": "postgres",
            })
        })
        .collect::<Vec<_>>();

    let (status, created, skipped, failed) = if options.standalone {
        let home = gobby_home().map_err(standalone_error)?;
        let mut service_options = DockerServiceOptions::new(home.clone());
        apply_service_overrides(&options, &mut service_options);
        let mut ensure_options = EnsureHubOptions::new(home.clone());
        ensure_options.service_options = service_options.clone();
        ensure_options.provision_services = !options.no_services;
        if let Some(database_url) = options.database_url.clone().or_else(database_url_from_env) {
            ensure_options.candidate_database_urls.push(database_url);
        }
        let (database_url, service_report) =
            ensure_hub(&ensure_options).map_err(standalone_error)?;
        let (created, skipped, failed) =
            run_gwiki_standalone_postgres_setup(&setup, &database_url)?;
        write_gwiki_gcore_config(
            &home,
            &options,
            &service_options,
            &database_url,
            service_report.as_ref(),
        )
        .map_err(standalone_error)?;
        let status = setup_status(&created, &skipped, &failed);
        (status, created, skipped, failed)
    } else if let Some(database_url) = options.database_url.clone().or_else(database_url_from_env) {
        let (created, skipped, failed) = validate_gwiki_postgres_setup(&database_url)?;
        let status = setup_status(&created, &skipped, &failed);
        (status, created, skipped, failed)
    } else {
        ("no_database", Vec::new(), Vec::new(), Vec::new())
    };

    Ok(render(
        output_scope,
        status,
        object_payloads,
        created,
        skipped,
        failed,
        wiki_setup::SETUP_OWNERSHIP_NOTE,
    ))
}

fn run_gwiki_standalone_postgres_setup(
    setup: &impl StandaloneSetup,
    database_url: &str,
) -> Result<PostgresSetupResult, WikiError> {
    let mut client = gobby_core::postgres::connect_readwrite(database_url).map_err(|error| {
        WikiError::Config {
            detail: format!("failed to connect to PostgreSQL for gwiki setup: {error}"),
        }
    })?;
    let mut ctx = SetupContext {
        pg: Some(&mut client),
        falkor_config: None,
        qdrant_config: None,
        non_interactive: true,
    };
    let report = setup.create(&mut ctx).map_err(WikiError::from)?;
    Ok((report.created, report.skipped, report.failed))
}

fn validate_gwiki_postgres_setup(database_url: &str) -> Result<PostgresSetupResult, WikiError> {
    let mut client = gobby_core::postgres::connect_readonly(database_url).map_err(|error| {
        WikiError::Config {
            detail: format!("failed to connect to PostgreSQL for gwiki setup validation: {error}"),
        }
    })?;
    let mut ctx = ValidationContext {
        pg: Some(&mut client),
        falkor_config: None,
        qdrant_config: None,
    };
    let report = crate::schema::validate_runtime_schema(&mut ctx);
    Ok((
        Vec::new(),
        report.present,
        report
            .missing
            .into_iter()
            .map(|(name, issue)| (name, issue.guidance.problem))
            .collect(),
    ))
}

fn apply_service_overrides(options: &SetupOptions, service_options: &mut DockerServiceOptions) {
    if let Some(host) = options.falkordb_host.as_deref() {
        service_options.falkordb_host = host.to_string();
    }
    if let Some(port) = options.falkordb_port {
        service_options.falkordb_port = port;
    }
    if let Some(password) = options.falkordb_password.as_deref() {
        service_options.falkordb_password = password.to_string();
    }
}

fn gobby_home() -> anyhow::Result<PathBuf> {
    gobby_core::gobby_home()
}

fn write_gwiki_gcore_config(
    home: &Path,
    options: &SetupOptions,
    service_options: &DockerServiceOptions,
    database_url: &str,
    service_report: Option<&DockerProvisioningReport>,
) -> anyhow::Result<PathBuf> {
    let path = gcore_config_path(home);
    let mut config = StandaloneConfig::read_at(&path)?.unwrap_or_else(StandaloneConfig::empty);

    config.set("databases.postgres.dsn", database_url);

    if let Some(report) = service_report {
        config.set("databases.falkordb.host", &service_options.falkordb_host);
        config.set(
            "databases.falkordb.port",
            service_options.falkordb_port.to_string(),
        );
        config.set(
            "databases.falkordb.password",
            &service_options.falkordb_password,
        );
        config.remove("databases.falkordb.requirepass");
        config.set("databases.qdrant.url", service_options.qdrant_url());
        config.set(
            "services.compose_file",
            report.compose_file.display().to_string(),
        );
    } else {
        if let Some(host) = options.falkordb_host.as_deref() {
            config.set("databases.falkordb.host", host);
        }
        if let Some(port) = options.falkordb_port {
            config.set("databases.falkordb.port", port.to_string());
        }
        if let Some(password) = options.falkordb_password.as_deref() {
            config.set("databases.falkordb.password", password);
            config.remove("databases.falkordb.requirepass");
        }
        if let Some(qdrant_url) = options.qdrant_url.as_deref() {
            config.set("databases.qdrant.url", qdrant_url);
        } else if compose_file_path(home).exists() {
            config.set(
                "services.compose_file",
                compose_file_path(home).display().to_string(),
            );
        }
    }

    apply_embedding_options(options, &mut config)?;
    config.write_at(&path)?;
    Ok(path)
}

fn apply_embedding_options(
    options: &SetupOptions,
    config: &mut StandaloneConfig,
) -> anyhow::Result<()> {
    if let Some(vector_dim) = options.embedding_vector_dim
        && !(1..=8192).contains(&vector_dim)
    {
        anyhow::bail!("--embedding-vector-dim must be between 1 and 8192");
    }
    let has_embedding_options = options.embedding_provider.is_some()
        || options.embedding_api_base.is_some()
        || options.embedding_model.is_some()
        || options.embedding_query_prefix.is_some()
        || options.embedding_vector_dim.is_some()
        || options.embedding_api_key.is_some();
    if !has_embedding_options {
        return Ok(());
    }
    for key in embedding_keys::legacy_keys() {
        config.remove(&key);
    }
    if let Some(provider) = options.embedding_provider.as_deref() {
        config.set(embedding_keys::AI_PROVIDER, provider);
    }
    if let Some(api_base) = options.embedding_api_base.as_deref() {
        config.set(embedding_keys::AI_API_BASE, api_base);
    }
    if let Some(model) = options.embedding_model.as_deref() {
        config.set(embedding_keys::AI_MODEL, model);
    }
    if let Some(query_prefix) = options.embedding_query_prefix.as_deref() {
        config.set(embedding_keys::AI_QUERY_PREFIX, query_prefix);
    }
    if let Some(vector_dim) = options.embedding_vector_dim {
        config.set(embedding_keys::AI_DIM, vector_dim.to_string());
    }
    if let Some(api_key) = options.embedding_api_key.as_deref() {
        config.set(embedding_keys::AI_API_KEY, api_key);
    }
    Ok(())
}

fn standalone_error(error: anyhow::Error) -> WikiError {
    WikiError::Config {
        detail: format!("standalone gwiki setup failed: {error}"),
    }
}

fn render(
    scope: ScopeIdentity,
    status: &str,
    objects: Vec<serde_json::Value>,
    created: Vec<String>,
    skipped: Vec<String>,
    failed: Vec<(String, String)>,
    ownership: &str,
) -> CommandOutcome {
    let object_count = objects.len();
    let payload = json!({
        "command": "setup",
        "scope": scope,
        "status": status,
        "objects": objects,
        "created": created,
        "skipped": skipped,
        "failed": failed,
        "ownership": ownership,
    });
    let text = format!(
        "Setup {status}
Scope: {scope}
Objects: {object_count}"
    );
    super::scoped_outcome("setup", &scope, payload, text)
}

fn setup_status(
    created: &[String],
    skipped: &[String],
    failed: &[(String, String)],
) -> &'static str {
    if !failed.is_empty() {
        "failed"
    } else if !created.is_empty() {
        "created"
    } else if !skipped.is_empty() {
        "already_present"
    } else {
        "ready"
    }
}

#[cfg(test)]
mod tests {
    use super::{apply_embedding_options, setup_status, write_gwiki_gcore_config};
    use crate::SetupOptions;
    use gobby_core::config::embedding_keys;
    use gobby_core::provisioning::{DockerServiceOptions, StandaloneConfig, gcore_config_path};

    #[test]
    fn setup_status_reports_specific_outcome() {
        assert_eq!(
            setup_status(&[], &[], &[("x".into(), "bad".into())]),
            "failed"
        );
        assert_eq!(setup_status(&["x".into()], &[], &[]), "created");
        assert_eq!(setup_status(&[], &["x".into()], &[]), "already_present");
        assert_eq!(setup_status(&[], &[], &[]), "ready");
    }

    #[test]
    fn standalone_provisions_and_merges_config() {
        let home = tempfile::tempdir().expect("temp home");
        let path = gcore_config_path(home.path());
        let mut existing = StandaloneConfig::empty();
        existing.set("code.index.schema", "code_index");
        existing.set(embedding_keys::AI_PROVIDER, "existing-provider");
        existing
            .write_at(&path)
            .expect("write existing standalone config");

        let options = SetupOptions {
            qdrant_url: Some("http://localhost:7333".to_string()),
            ..SetupOptions::default()
        };
        let service_options = DockerServiceOptions::new(home.path().to_path_buf());

        write_gwiki_gcore_config(
            home.path(),
            &options,
            &service_options,
            "postgresql://localhost/gwiki",
            None,
        )
        .expect("write gwiki gcore config");

        let config = StandaloneConfig::read_at(&path)
            .expect("read config")
            .expect("config present");
        assert_eq!(
            config.get("databases.postgres.dsn"),
            Some("postgresql://localhost/gwiki")
        );
        assert_eq!(
            config.get("databases.qdrant.url"),
            Some("http://localhost:7333")
        );
        assert_eq!(config.get("code.index.schema"), Some("code_index"));
        assert_eq!(
            config.get(embedding_keys::AI_PROVIDER),
            Some("existing-provider")
        );
    }

    #[test]
    fn reuses_existing_gcode_hub() {
        let home = tempfile::tempdir().expect("temp home");
        let path = gcore_config_path(home.path());
        let mut existing = StandaloneConfig::empty();
        existing.set("databases.postgres.dsn", "postgresql://localhost/gcode");
        existing.set("code.index.schema", "public");
        existing.write_at(&path).expect("write existing config");

        let service_options = DockerServiceOptions::new(home.path().to_path_buf());
        write_gwiki_gcore_config(
            home.path(),
            &SetupOptions::default(),
            &service_options,
            "postgresql://localhost/gcode",
            None,
        )
        .expect("merge gwiki config");

        let config = StandaloneConfig::read_at(&path)
            .expect("read config")
            .expect("config present");
        assert_eq!(
            config.get("databases.postgres.dsn"),
            Some("postgresql://localhost/gcode")
        );
        assert_eq!(config.get("code.index.schema"), Some("public"));
    }

    #[test]
    fn invalid_embedding_dim_does_not_mutate_config() {
        for vector_dim in [0, 8193] {
            let mut config = StandaloneConfig::empty();
            config.set(embedding_keys::AI_PROVIDER, "existing-provider");
            let options = SetupOptions {
                embedding_provider: Some("new-provider".to_string()),
                embedding_vector_dim: Some(vector_dim),
                ..SetupOptions::default()
            };

            let error =
                apply_embedding_options(&options, &mut config).expect_err("invalid dim rejected");

            assert!(error.to_string().contains("between 1 and 8192"));
            assert_eq!(
                config.get(embedding_keys::AI_PROVIDER),
                Some("existing-provider")
            );
        }
    }
}
