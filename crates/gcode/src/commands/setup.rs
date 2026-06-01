use anyhow::Context as _;
use gobby_core::config::embedding_keys;
use gobby_core::provisioning::{
    DEFAULT_EMBEDDING_VECTOR_DIM, DEFAULT_LM_STUDIO_API_BASE, DEFAULT_OLLAMA_API_BASE,
    DEFAULT_OLLAMA_MODEL, DockerProvisioningReport, DockerServiceOptions, EmbeddingBootstrap,
    EnsureHubOptions, StandaloneConfig, compose_file_path, ensure_hub, gcore_config_path,
};
use postgres::{Client, NoTls};
use std::net::{TcpStream, ToSocketAddrs};
use std::time::Duration;

use crate::config::{self, QdrantConfig};
use crate::db;
use crate::graph::code_graph;
use crate::output::{self, Format};
use crate::setup::{
    self, StandaloneEmbeddingStatus, StandaloneServicesStatus, StandaloneSetupRequest,
};
use crate::utils::api_key_fingerprint;
use crate::vector::code_symbols;

pub fn run(request: StandaloneSetupRequest, format: Format, quiet: bool) -> anyhow::Result<()> {
    setup::validate_standalone_request(&request)?;

    let home = db::gobby_home()?;
    let mut service_options = DockerServiceOptions::new(home.clone());
    apply_service_overrides(&request, &mut service_options);

    let embedding = resolve_embedding_bootstrap(&request)?;
    let (database_url, service_report) = resolve_or_provision_database(&request, &service_options)?;
    let mut client = connect_postgres_with_retry(&database_url, service_report.is_some())?;
    if request.overwrite_code_index {
        clear_overwrite_projections(&home, &request, &service_options, service_report.as_ref())?;
    }
    let mut status = setup::run_standalone_setup(&request, &mut client)?;
    if !status.failed.is_empty() {
        match format {
            Format::Json => {
                output::print_json(&status)?;
            }
            Format::Text => {
                for failure in &status.failed {
                    eprintln!("Failed to create {}: {}", failure.name, failure.reason);
                }
            }
        }
        anyhow::bail!("standalone gcode setup failed");
    }

    let config_file = write_gcore_config(
        &home,
        &request,
        &service_options,
        &database_url,
        service_report.as_ref(),
        embedding.as_ref(),
    )?;
    status.config_file = Some(config_file.display().to_string());
    status.services = Some(match service_report {
        Some(report) => StandaloneServicesStatus {
            provisioned: true,
            compose_file: Some(report.compose_file.display().to_string()),
            health_checks: report.health_checks,
        },
        None => StandaloneServicesStatus {
            provisioned: false,
            compose_file: service_configured_compose_file(&home),
            health_checks: Vec::new(),
        },
    });
    status.embedding = embedding.map(|embedding| StandaloneEmbeddingStatus {
        provider: embedding.provider,
        api_base: embedding.api_base,
        model: embedding.model,
        query_prefix: embedding.query_prefix,
        vector_dim: embedding.vector_dim,
        api_key_present: embedding.api_key.is_some(),
        api_key_fingerprint: embedding.api_key.as_deref().map(api_key_fingerprint),
    });

    match format {
        Format::Json => output::print_json(&status),
        Format::Text => {
            if !quiet {
                output::print_text(&format!(
                    "Standalone gcode setup complete in schema {}",
                    status.schema
                ))?;
            }
            Ok(())
        }
    }
}

struct OverwriteProjectionConfigs {
    falkordb: Option<config::FalkorConfig>,
    qdrant: Option<QdrantConfig>,
}

fn clear_overwrite_projections(
    home: &std::path::Path,
    request: &StandaloneSetupRequest,
    service_options: &DockerServiceOptions,
    service_report: Option<&DockerProvisioningReport>,
) -> anyhow::Result<()> {
    let configs = overwrite_projection_configs(home, request, service_options, service_report)?;
    if let Some(falkordb) = configs.falkordb {
        code_graph::clear_all_code_index(&falkordb)
            .context("failed to clear FalkorDB code-index projection during overwrite setup")?;
    }
    if let Some(qdrant) = configs.qdrant {
        code_symbols::delete_code_symbol_collections_with_prefix(&qdrant)
            .context("failed to delete Qdrant code-symbol collections during overwrite setup")?;
    }
    Ok(())
}

fn overwrite_projection_configs(
    home: &std::path::Path,
    request: &StandaloneSetupRequest,
    service_options: &DockerServiceOptions,
    service_report: Option<&DockerProvisioningReport>,
) -> anyhow::Result<OverwriteProjectionConfigs> {
    let mut standalone = StandaloneConfig::read_at(&gcore_config_path(home))?
        .unwrap_or_else(StandaloneConfig::empty);

    if service_report.is_some() {
        standalone.set("databases.falkordb.host", &service_options.falkordb_host);
        standalone.set(
            "databases.falkordb.port",
            service_options.falkordb_port.to_string(),
        );
        standalone.set(
            "databases.falkordb.password",
            &service_options.falkordb_password,
        );
        standalone.set("databases.qdrant.url", service_options.qdrant_url());
    }

    if let Some(host) = request.falkordb_host.as_deref() {
        standalone.set("databases.falkordb.host", host);
    }
    if let Some(port) = request.falkordb_port {
        standalone.set("databases.falkordb.port", port.to_string());
    }
    if let Some(password) = request.falkordb_password.as_deref() {
        standalone.set("databases.falkordb.password", password);
    }
    if let Some(qdrant_url) = request.qdrant_url.as_deref() {
        standalone.set("databases.qdrant.url", qdrant_url);
    }

    let falkordb = gobby_core::config::resolve_falkordb_config(&mut standalone).map(|connection| {
        config::FalkorConfig {
            host: connection.host,
            port: connection.port,
            password: connection.password,
            graph_name: config::FALKORDB_GRAPH_NAME.to_string(),
        }
    });
    let qdrant = gobby_core::config::resolve_qdrant_config(&mut standalone);

    Ok(OverwriteProjectionConfigs { falkordb, qdrant })
}

fn resolve_or_provision_database(
    request: &StandaloneSetupRequest,
    service_options: &DockerServiceOptions,
) -> anyhow::Result<(String, Option<DockerProvisioningReport>)> {
    if let Some(database_url) = request.database_url.as_deref() {
        return Ok((database_url.to_string(), None));
    }

    if request.no_services {
        return db::resolve_database_url().map(|url| (url, None));
    }

    let home = db::gobby_home()?;
    let mut options = EnsureHubOptions::new(home);
    options.service_options = service_options.clone();
    if let Ok(database_url) = db::resolve_database_url() {
        options.candidate_database_urls.push(database_url);
    }
    ensure_hub(&options)
}

fn apply_service_overrides(
    request: &StandaloneSetupRequest,
    service_options: &mut DockerServiceOptions,
) {
    if let Some(host) = request.falkordb_host.as_deref() {
        service_options.falkordb_host = host.to_string();
    }
    if let Some(port) = request.falkordb_port {
        service_options.falkordb_port = port;
    }
    if let Some(password) = request.falkordb_password.as_deref() {
        service_options.falkordb_password = password.to_string();
    }
}

fn connect_postgres_with_retry(database_url: &str, retry: bool) -> anyhow::Result<Client> {
    let attempts = if retry { 30 } else { 1 };
    let mut last_error = None;
    for attempt in 0..attempts {
        match Client::connect(database_url, NoTls) {
            Ok(client) => return Ok(client),
            Err(err) => last_error = Some(err),
        }
        if attempt + 1 < attempts {
            std::thread::sleep(Duration::from_secs(2));
        }
    }
    match last_error {
        Some(err) => Err(anyhow::Error::new(err)
            .context("failed to connect to the standalone PostgreSQL database")),
        None => anyhow::bail!("failed to connect to the standalone PostgreSQL database"),
    }
}

fn write_gcore_config(
    home: &std::path::Path,
    request: &StandaloneSetupRequest,
    service_options: &DockerServiceOptions,
    database_url: &str,
    service_report: Option<&DockerProvisioningReport>,
    embedding: Option<&EmbeddingBootstrap>,
) -> anyhow::Result<std::path::PathBuf> {
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
        if let Some(host) = request.falkordb_host.as_deref() {
            config.set("databases.falkordb.host", host);
        }
        if let Some(port) = request.falkordb_port {
            config.set("databases.falkordb.port", port.to_string());
        }
        if let Some(password) = request.falkordb_password.as_deref() {
            config.set("databases.falkordb.password", password);
            config.remove("databases.falkordb.requirepass");
        }
        if let Some(qdrant_url) = request.qdrant_url.as_deref() {
            config.set("databases.qdrant.url", qdrant_url);
        }
    }

    if let Some(embedding) = embedding {
        remove_legacy_embedding_keys(&mut config);
        config.set(embedding_keys::AI_PROVIDER, &embedding.provider);
        config.set(embedding_keys::AI_API_BASE, &embedding.api_base);
        config.set(embedding_keys::AI_MODEL, &embedding.model);
        config.set(embedding_keys::AI_DIM, embedding.vector_dim.to_string());
        match embedding.query_prefix.as_deref() {
            Some(query_prefix) => config.set(embedding_keys::AI_QUERY_PREFIX, query_prefix),
            None => config.remove(embedding_keys::AI_QUERY_PREFIX),
        }
        match embedding.api_key.as_deref() {
            Some(api_key) => config.set(embedding_keys::AI_API_KEY, api_key),
            None => config.remove(embedding_keys::AI_API_KEY),
        }
    }

    config.write_at(&path)?;
    Ok(path)
}

fn remove_legacy_embedding_keys(config: &mut StandaloneConfig) {
    for key in embedding_keys::legacy_keys() {
        config.remove(&key);
    }
}

fn service_configured_compose_file(home: &std::path::Path) -> Option<String> {
    let compose = compose_file_path(home);
    compose.exists().then(|| compose.display().to_string())
}

fn resolve_embedding_bootstrap(
    request: &StandaloneSetupRequest,
) -> anyhow::Result<Option<EmbeddingBootstrap>> {
    let provider = request
        .embedding_provider
        .as_deref()
        .map(|provider| provider.trim().to_ascii_lowercase());

    let mut embedding = match provider.as_deref() {
        Some("none") => return Ok(None),
        Some("lm-studio") | Some("lmstudio") => EmbeddingBootstrap::lm_studio(),
        Some("ollama") => EmbeddingBootstrap::ollama(),
        Some("openai-compatible") | Some("openai") | Some("remote") => {
            explicit_embedding_bootstrap(request)?
        }
        Some(other) => anyhow::bail!(
            "unsupported embedding provider `{other}`; expected lm-studio, ollama, openai-compatible, or none"
        ),
        None if request.embedding_api_base.is_some()
            || request.embedding_model.is_some()
            || request.embedding_query_prefix.is_some()
            || request.embedding_api_key.is_some() =>
        {
            explicit_embedding_bootstrap(request)?
        }
        None if endpoint_reachable(DEFAULT_LM_STUDIO_API_BASE) => EmbeddingBootstrap::lm_studio(),
        None if endpoint_reachable(DEFAULT_OLLAMA_API_BASE) => EmbeddingBootstrap::ollama(),
        None => EmbeddingBootstrap::lm_studio(),
    };

    if let Some(api_base) = request.embedding_api_base.as_deref() {
        embedding.api_base = api_base.to_string();
    }
    if let Some(model) = request.embedding_model.as_deref() {
        embedding.model = model.to_string();
    }
    if let Some(query_prefix) = request.embedding_query_prefix.as_deref() {
        embedding.query_prefix = Some(query_prefix.to_string());
    }
    if let Some(vector_dim) = request.embedding_vector_dim {
        if vector_dim == 0 {
            anyhow::bail!("--embedding-vector-dim must be positive");
        }
        embedding.vector_dim = vector_dim;
    }
    if let Some(api_key) = request.embedding_api_key.as_deref() {
        embedding.api_key = Some(api_key.to_string());
    }

    Ok(Some(embedding))
}

fn explicit_embedding_bootstrap(
    request: &StandaloneSetupRequest,
) -> anyhow::Result<EmbeddingBootstrap> {
    let Some(api_base) = request.embedding_api_base.as_deref() else {
        anyhow::bail!("--embedding-api-base is required for openai-compatible embeddings");
    };
    Ok(EmbeddingBootstrap {
        provider: "openai-compatible".to_string(),
        api_base: api_base.to_string(),
        model: request
            .embedding_model
            .clone()
            .unwrap_or_else(|| DEFAULT_OLLAMA_MODEL.to_string()),
        vector_dim: request
            .embedding_vector_dim
            .unwrap_or(DEFAULT_EMBEDDING_VECTOR_DIM),
        query_prefix: request.embedding_query_prefix.clone(),
        api_key: request.embedding_api_key.clone(),
    })
}

fn endpoint_reachable(api_base: &str) -> bool {
    let Ok(url) = reqwest::Url::parse(api_base) else {
        return false;
    };
    let Some(host) = url.host_str() else {
        return false;
    };
    let Some(port) = url.port_or_known_default() else {
        return false;
    };
    let Ok(addrs) = (host, port).to_socket_addrs() else {
        return false;
    };
    addrs
        .into_iter()
        .any(|addr| TcpStream::connect_timeout(&addr, Duration::from_millis(150)).is_ok())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    #[test]
    fn write_gcore_config_writes_ai_embeddings_and_redacts_api_key() {
        let home = tempfile::tempdir().expect("temp home");
        let path = gcore_config_path(home.path());
        let legacy_keys = embedding_keys::legacy_keys();
        let mut existing = StandaloneConfig::empty();
        existing.set(legacy_keys[1].clone(), "http://legacy.local/v1");
        existing
            .write_at(&path)
            .expect("write existing standalone config");

        let request = StandaloneSetupRequest::new(
            true,
            Some("postgresql://localhost/gobby".to_string()),
            None,
        );
        let service_options = DockerServiceOptions::new(home.path().to_path_buf());
        let embedding = EmbeddingBootstrap {
            provider: "openai-compatible".to_string(),
            api_base: "http://localhost:1234/v1".to_string(),
            model: "embed-small".to_string(),
            vector_dim: 1024,
            query_prefix: Some("query: ".to_string()),
            api_key: Some("local-api-key".to_string()),
        };

        let path = write_gcore_config(
            home.path(),
            &request,
            &service_options,
            "postgresql://localhost/gobby",
            None,
            Some(&embedding),
        )
        .expect("write gcore config");
        let config = StandaloneConfig::read_at(&path)
            .expect("read gcore config")
            .expect("config present");

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
        for key in legacy_keys {
            assert_eq!(config.get(&key), None, "legacy key survived: {key}");
        }

        let status = StandaloneEmbeddingStatus {
            provider: embedding.provider,
            api_base: embedding.api_base,
            model: embedding.model,
            query_prefix: embedding.query_prefix,
            vector_dim: embedding.vector_dim,
            api_key_present: embedding.api_key.is_some(),
            api_key_fingerprint: embedding.api_key.as_deref().map(api_key_fingerprint),
        };
        let output = serde_json::to_value(status).expect("serialize status");
        assert_eq!(output["api_key_present"], Value::Bool(true));
        assert_eq!(
            output["api_key_fingerprint"],
            Value::String(api_key_fingerprint("local-api-key"))
        );
        assert!(
            !output.to_string().contains("local-api-key"),
            "setup status leaked plaintext API key"
        );
    }

    #[test]
    #[serial_test::serial]
    fn standalone_command_installs_public_code_index_subset() {
        let Ok(database_url) = std::env::var("GCODE_POSTGRES_TEST_DATABASE_URL") else {
            return;
        };
        let home = tempfile::tempdir().expect("temp home");
        unsafe { std::env::set_var("GOBBY_HOME", home.path()) };
        let request = StandaloneSetupRequest::new(true, Some(database_url.clone()), None);

        run(request, Format::Json, true).expect("standalone setup runs");

        let mut client =
            postgres::Client::connect(&database_url, postgres::NoTls).expect("connect test db");
        let exists: bool = client
            .query_one("SELECT to_regclass('public.code_symbols') IS NOT NULL", &[])
            .expect("check code_symbols")
            .get(0);
        assert!(exists);

        let forbidden_exists: bool = client
            .query_one("SELECT to_regclass('public.config_store') IS NOT NULL", &[])
            .expect("check config_store")
            .get(0);
        assert!(!forbidden_exists);
        assert!(home.path().join("gcore.yaml").exists());

        client
            .batch_execute(
                "DROP INDEX IF EXISTS public.code_symbols_search_bm25;
                 DROP INDEX IF EXISTS public.code_content_search_bm25;
                 DROP TABLE IF EXISTS public.code_calls;
                 DROP TABLE IF EXISTS public.code_imports;
                 DROP TABLE IF EXISTS public.code_content_chunks;
                 DROP TABLE IF EXISTS public.code_symbols;
                 DROP TABLE IF EXISTS public.code_indexed_files;
                 DROP TABLE IF EXISTS public.code_indexed_projects;",
            )
            .expect("drop code-index test objects");
        unsafe { std::env::remove_var("GOBBY_HOME") };
    }
}
