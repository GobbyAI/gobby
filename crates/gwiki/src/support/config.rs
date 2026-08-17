use gobby_core::ai::effective_config::{
    EffectiveConfigLayers, ai_source_with_secret_primary, daemon_mode_layers,
};
use gobby_core::config::{
    ConfigSource, DaemonOrPrimary, EnvOnlySource, QdrantConfig, resolve_indexing_config,
};
use postgres::Client;

use crate::indexer::IndexOptions;
use crate::{WikiError, indexer};

use super::search::PostgresConfigSource;

type HubAiConfigSource = gobby_core::ai_context::AiConfigSource<DaemonOrPrimary<HubPrimary>>;
type HubPrimaryFactory<'a> = Box<dyn FnOnce() -> anyhow::Result<HubPrimary> + 'a>;

/// Hub-backed primary AI config layer with a lazily-opened connection.
///
/// Commands that synthesize daemon-independently still see `${secret}:`
/// references (the canonical api_key marker). `resolve_value` rejects those
/// markers rather than resolving them; the daemon grant issuer substitutes
/// valid secret-store references. Without a hub, plain values pass through.
/// Construction is I/O-free — the connection opens on first hub-needing
/// read — so daemon mode can keep this primary attached without every command
/// paying for (or failing on) a hub connection it never uses.
pub(crate) struct HubPrimary {
    command: String,
    conn: Option<HubConnState>,
}

enum HubConnState {
    Open(Box<Client>),
    Unavailable(String),
}

impl HubPrimary {
    fn new(command: &str) -> Self {
        Self {
            command: command.to_string(),
            conn: None,
        }
    }

    fn state(&mut self) -> &mut HubConnState {
        let command = &self.command;
        self.conn
            .get_or_insert_with(|| match super::env::database_url_for(command) {
                Ok(Some(url)) => match gobby_core::postgres::connect_readwrite(&url) {
                    Ok(client) => HubConnState::Open(Box::new(client)),
                    Err(error) => {
                        HubConnState::Unavailable(format!("hub connection failed: {error}"))
                    }
                },
                Ok(None) => {
                    HubConnState::Unavailable("no hub database_url is configured".to_string())
                }
                Err(error) => HubConnState::Unavailable(format!(
                    "hub database_url resolution failed: {error}"
                )),
            })
    }
}

impl ConfigSource for HubPrimary {
    fn snapshot_revision(&mut self) -> anyhow::Result<Option<i64>> {
        match self.state() {
            HubConnState::Open(conn) => {
                gobby_core::postgres::read_config_revision(conn.as_mut()).map(Some)
            }
            HubConnState::Unavailable(cause) => {
                anyhow::bail!(
                    "runtime configuration revision requires the PostgreSQL hub ({cause})"
                )
            }
        }
    }

    fn config_value(&mut self, key: &str) -> Option<String> {
        let HubConnState::Open(conn) = self.state() else {
            return None;
        };
        gobby_core::postgres::read_config_value(conn.as_mut(), key)
            .ok()
            .flatten()
            .and_then(|raw| gobby_core::config::decode_config_value(&raw))
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        // Reject `${secret}:` markers. This layer does not resolve secret-store
        // references; the daemon grant issuer substitutes them before they reach
        // the client. Plain values are returned unchanged.
        gobby_core::config::reject_secret_marker(value)?;
        Ok(value.to_string())
    }
}

pub(crate) fn hub_ai_config_source(command: &str) -> Result<HubAiConfigSource, WikiError> {
    hub_ai_config_source_with(
        command,
        Box::new(move || Ok(HubPrimary::new(command))),
        ai_source_with_secret_primary,
    )
}

fn hub_ai_config_source_with<'a>(
    command: &str,
    primary: HubPrimaryFactory<'a>,
    build_source: impl FnOnce(HubPrimaryFactory<'a>) -> anyhow::Result<HubAiConfigSource>,
) -> Result<HubAiConfigSource, WikiError> {
    build_source(primary).map_err(|error| WikiError::Config {
        detail: format!("failed to resolve AI config for {command}: {error}"),
    })
}

pub(crate) const DEFAULT_SHARED_CODE_GRAPH_EDGE_LIMIT: usize = 200;
const SHARED_CODE_CALL_EDGE_LIMIT_KEY: &str = "gwiki.shared_code.call_edge_limit";
const SHARED_CODE_IMPORT_EDGE_LIMIT_KEY: &str = "gwiki.shared_code.import_edge_limit";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct SharedCodeGraphLimits {
    pub(crate) call_edge_limit: usize,
    pub(crate) import_edge_limit: usize,
}

impl Default for SharedCodeGraphLimits {
    fn default() -> Self {
        Self {
            call_edge_limit: DEFAULT_SHARED_CODE_GRAPH_EDGE_LIMIT,
            import_edge_limit: DEFAULT_SHARED_CODE_GRAPH_EDGE_LIMIT,
        }
    }
}

pub(crate) fn local_index_options() -> Result<IndexOptions, WikiError> {
    resolve_index_options_from_layers(read_effective_config_layers()?, EnvOnlySource)
}

pub(crate) fn index_options_from_conn(conn: &mut Client) -> Result<IndexOptions, WikiError> {
    let primary = PostgresConfigSource { conn };
    resolve_index_options_from_layers(read_effective_config_layers()?, primary)
}

pub(crate) fn shared_code_graph_limits_from_conn(
    conn: &mut Client,
) -> Result<SharedCodeGraphLimits, WikiError> {
    let primary = PostgresConfigSource { conn };
    resolve_shared_code_graph_limits_from_layers(read_effective_config_layers()?, primary)
}

pub(crate) fn qdrant_config_has_url(config: &QdrantConfig) -> bool {
    config
        .url
        .as_deref()
        .is_some_and(|url| !url.trim().is_empty())
}

fn read_effective_config_layers() -> Result<Option<EffectiveConfigLayers>, WikiError> {
    daemon_mode_layers()
        .map(Some)
        .map_err(|error| WikiError::Config {
            detail: format!("failed to read daemon effective config for gwiki: {error}"),
        })
}

fn resolve_index_options_from_layers<P: ConfigSource>(
    layers: Option<EffectiveConfigLayers>,
    primary: P,
) -> Result<IndexOptions, WikiError> {
    resolve_from_layers(layers, primary, resolve_index_options)
}

fn resolve_shared_code_graph_limits_from_layers<P: ConfigSource>(
    layers: Option<EffectiveConfigLayers>,
    primary: P,
) -> Result<SharedCodeGraphLimits, WikiError> {
    resolve_from_layers(layers, primary, resolve_shared_code_graph_limits)
}

fn resolve_from_layers<P: ConfigSource, T>(
    layers: Option<EffectiveConfigLayers>,
    primary: P,
    resolve: impl FnOnce(&mut DaemonOrPrimary<P>) -> Result<T, WikiError>,
) -> Result<T, WikiError> {
    let mut source = match layers {
        Some(served) => DaemonOrPrimary::Daemon(served),
        None => DaemonOrPrimary::Primary(primary),
    };
    resolve(&mut source)
}

fn resolve_index_options(
    source: &mut impl gobby_core::config::ConfigSource,
) -> Result<IndexOptions, WikiError> {
    let config = resolve_indexing_config(source).map_err(|error| WikiError::Config {
        detail: format!("failed to resolve gwiki indexing config: {error}"),
    })?;
    Ok(index_options_from_config(config))
}

fn index_options_from_config(config: gobby_core::config::IndexingConfig) -> indexer::IndexOptions {
    indexer::IndexOptions {
        respect_gitignore: config.respect_gitignore,
        // Forcing is a per-invocation decision (CLI flag), never configuration.
        force: false,
    }
}

fn resolve_shared_code_graph_limits(
    source: &mut impl ConfigSource,
) -> Result<SharedCodeGraphLimits, WikiError> {
    Ok(SharedCodeGraphLimits {
        call_edge_limit: resolve_limit(source, SHARED_CODE_CALL_EDGE_LIMIT_KEY)?,
        import_edge_limit: resolve_limit(source, SHARED_CODE_IMPORT_EDGE_LIMIT_KEY)?,
    })
}

fn resolve_limit(source: &mut impl ConfigSource, key: &'static str) -> Result<usize, WikiError> {
    let Some(raw) = source.config_value(key) else {
        return Ok(DEFAULT_SHARED_CODE_GRAPH_EDGE_LIMIT);
    };
    let resolved = source
        .resolve_value(&raw)
        .map_err(|error| WikiError::Config {
            detail: format!("failed to resolve {key}: {error}"),
        })?;
    resolved
        .trim()
        .parse::<usize>()
        .map_err(|_| WikiError::Config {
            detail: format!("invalid non-negative integer for {key}: `{resolved}`"),
        })
}

#[cfg(test)]
mod tests {
    use std::cell::Cell;
    use std::collections::BTreeMap;
    use std::path::Path;

    use gobby_core::config::{DaemonOrPrimary, DaemonServedConfig};

    use crate::store::FakeWikiStore;

    use super::*;

    fn write_file(root: &Path, rel: &str, contents: &str) {
        let path = root.join(rel);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).expect("create parent");
        }
        std::fs::write(path, contents).expect("write file");
    }

    #[derive(Default)]
    struct TestSource {
        values: BTreeMap<String, String>,
    }

    impl TestSource {
        fn with(mut self, key: &str, value: &str) -> Self {
            self.values.insert(key.to_string(), value.to_string());
            self
        }
    }

    impl gobby_core::config::ConfigSource for TestSource {
        fn config_value(&mut self, key: &str) -> Option<String> {
            self.values.get(key).cloned()
        }

        fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
            Ok(value.to_string())
        }
    }

    #[test]
    fn shared_code_graph_limits_default_to_200() {
        let mut source = TestSource::default();

        let limits = resolve_shared_code_graph_limits(&mut source).expect("limits");

        assert_eq!(
            limits,
            SharedCodeGraphLimits {
                call_edge_limit: 200,
                import_edge_limit: 200,
            }
        );
    }

    #[test]
    fn shared_code_graph_limits_use_grant_backed_source() {
        let mut source = TestSource::default()
            .with(SHARED_CODE_CALL_EDGE_LIMIT_KEY, "11")
            .with(SHARED_CODE_IMPORT_EDGE_LIMIT_KEY, "12");

        let limits = resolve_shared_code_graph_limits(&mut source).expect("limits");

        assert_eq!(
            limits,
            SharedCodeGraphLimits {
                call_edge_limit: 11,
                import_edge_limit: 12,
            }
        );
    }

    #[test]
    fn shared_code_graph_limits_read_grant_backed_values() {
        let source = TestSource::default()
            .with(SHARED_CODE_CALL_EDGE_LIMIT_KEY, "31")
            .with(SHARED_CODE_IMPORT_EDGE_LIMIT_KEY, "32");
        let limits = resolve_shared_code_graph_limits_from_layers(None, source).expect("limits");

        assert_eq!(
            limits,
            SharedCodeGraphLimits {
                call_edge_limit: 31,
                import_edge_limit: 32,
            }
        );
    }

    #[test]
    fn shared_code_graph_limits_reject_invalid_or_negative_values() {
        let mut invalid = TestSource::default().with(SHARED_CODE_CALL_EDGE_LIMIT_KEY, "many");
        let error = resolve_shared_code_graph_limits(&mut invalid).expect_err("invalid limit");
        assert!(error.to_string().contains(SHARED_CODE_CALL_EDGE_LIMIT_KEY));

        let mut negative = TestSource::default().with(SHARED_CODE_IMPORT_EDGE_LIMIT_KEY, "-1");
        let error = resolve_shared_code_graph_limits(&mut negative).expect_err("negative limit");
        assert!(
            error
                .to_string()
                .contains(SHARED_CODE_IMPORT_EDGE_LIMIT_KEY)
        );
    }

    #[test]
    fn grant_backed_index_options_read_served_values() {
        let source = TestSource::default().with("indexing.respect_gitignore", "false");
        let options = resolve_index_options_from_layers(None, source).expect("index options");
        assert!(!options.respect_gitignore);
    }

    #[test]
    fn daemon_index_options_use_served_values() {
        let served = DaemonServedConfig::new(
            7,
            BTreeMap::from([(
                "indexing.respect_gitignore".to_string(),
                "false".to_string(),
            )]),
        );

        let options = resolve_index_options_from_layers(Some(served), TestSource::default())
            .expect("daemon index options");

        assert!(!options.respect_gitignore);
    }

    #[test]
    fn daemon_hub_ai_source_construction_opens_no_database_connection() {
        // Daemon mode now keeps the hub primary attached for secret-reference
        // fall-through, so the factory runs at construction — but building a
        // `HubPrimary` is I/O-free (the connection opens on first hub-needing
        // read), and non-secret reads never touch it.
        let factory_ran = Cell::new(false);

        let mut source = hub_ai_config_source_with(
            "test",
            Box::new(|| {
                factory_ran.set(true);
                Ok(HubPrimary::new("test"))
            }),
            |primary| {
                Ok(gobby_core::ai_context::AiConfigSource::with_primary(
                    DaemonOrPrimary::DaemonWithSecrets(
                        DaemonServedConfig::new(
                            7,
                            BTreeMap::from([(
                                "ai.embeddings.model".to_string(),
                                "daemon-model".to_string(),
                            )]),
                        ),
                        primary()?,
                    ),
                ))
            },
        )
        .expect("daemon AI source");

        assert!(factory_ran.get());
        // Served values and non-secret resolution stay on the daemon side and
        // never open the lazy hub connection.
        assert_eq!(
            source.config_value("ai.embeddings.model").as_deref(),
            Some("daemon-model")
        );
        assert_eq!(
            source
                .resolve_value("plain-value")
                .expect("non-secret resolution"),
            "plain-value"
        );
    }

    #[test]
    fn plain_value_resolution_does_not_initialize_hub_connection() {
        let mut primary = HubPrimary::new("test");
        assert_eq!(
            primary.resolve_value("plain-value").expect("plain value"),
            "plain-value"
        );
        assert!(
            primary.conn.is_none(),
            "plain-value resolution must not open a hub connection"
        );
    }

    #[test]
    fn memory_indexing_uses_injected_grant_backed_options() {
        let source = TestSource::default().with("indexing.respect_gitignore", "false");
        let options = resolve_index_options_from_layers(None, source).expect("index options");

        let vault = tempfile::tempdir().expect("vault");
        std::fs::create_dir(vault.path().join(".git")).expect("git dir");
        write_file(vault.path(), ".gitignore", "knowledge/topics/ignored.md\n");
        write_file(vault.path(), "knowledge/topics/ignored.md", "# Ignored\n");

        let mut store = FakeWikiStore::default();
        crate::indexer::index_vault(
            vault.path(),
            &mut store,
            options,
            &mut crate::progress::ProgressOptions::default(),
        )
        .expect("index vault");

        assert!(
            store
                .documents
                .contains_key(&std::path::PathBuf::from("knowledge/topics/ignored.md"))
        );
    }
}
