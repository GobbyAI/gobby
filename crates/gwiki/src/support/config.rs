use gobby_core::ai::effective_config::{
    EffectiveConfigLayers, ai_source_with_primary, daemon_mode_layers,
};
use gobby_core::config::{
    ConfigSource, DaemonOrPrimary, EnvOnlySource, LayeredConfigSource, QdrantConfig,
    resolve_indexing_config,
};
use gobby_core::provisioning::{StandaloneConfig, gcore_config_path};
use postgres::Client;

use crate::indexer::IndexOptions;
use crate::{WikiError, indexer};

use super::search::PostgresConfigSource;

type HubAiConfigSource = gobby_core::ai_context::AiConfigSource<DaemonOrPrimary<HubPrimary>>;
type HubPrimaryFactory<'a> = Box<dyn FnOnce() -> anyhow::Result<HubPrimary> + 'a>;

/// Hub-backed primary AI config layer with an owned, optional connection.
///
/// Commands that synthesize daemon-independently still need `$secret:`
/// references (the canonical api_key pattern) to resolve through the
/// PostgreSQL hub when it is reachable; without a hub, plain values resolve
/// and secrets degrade explicitly.
pub(crate) struct HubPrimary {
    conn: Option<Client>,
}

impl ConfigSource for HubPrimary {
    fn config_value(&mut self, key: &str) -> Option<String> {
        let conn = self.conn.as_mut()?;
        gobby_core::postgres::read_config_value(conn, key)
            .ok()
            .flatten()
            .and_then(|raw| gobby_core::config::decode_config_value(&raw))
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        match self.conn.as_mut() {
            Some(conn) => gobby_core::secrets::resolve_config_value(value, conn),
            None => {
                if value.trim_start().starts_with("$secret:") {
                    anyhow::bail!(
                        "secret resolution requires the PostgreSQL hub; configure the hub or use a literal api_key"
                    );
                }
                Ok(value.to_string())
            }
        }
    }
}

pub(crate) fn hub_ai_config_source(command: &str) -> Result<HubAiConfigSource, WikiError> {
    hub_ai_config_source_with(
        command,
        Box::new(move || {
            let conn = super::env::database_url_for(command)?
                .and_then(|url| gobby_core::postgres::connect_readwrite(&url).ok());
            Ok(HubPrimary { conn })
        }),
        ai_source_with_primary,
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
    resolve_index_options_from_layers(
        read_effective_config_layers()?,
        EnvOnlySource,
        read_standalone_config,
    )
}

pub(crate) fn index_options_from_conn(conn: &mut Client) -> Result<IndexOptions, WikiError> {
    let primary = PostgresConfigSource { conn };
    resolve_index_options_from_layers(
        read_effective_config_layers()?,
        primary,
        read_standalone_config,
    )
}

#[cfg(test)]
pub(crate) fn local_shared_code_graph_limits() -> Result<SharedCodeGraphLimits, WikiError> {
    let standalone = read_standalone_config()?;
    match standalone {
        Some(mut source) => resolve_shared_code_graph_limits(&mut source),
        None => Ok(SharedCodeGraphLimits::default()),
    }
}

pub(crate) fn shared_code_graph_limits_from_conn(
    conn: &mut Client,
) -> Result<SharedCodeGraphLimits, WikiError> {
    let primary = PostgresConfigSource { conn };
    resolve_shared_code_graph_limits_from_layers(
        read_effective_config_layers()?,
        primary,
        read_standalone_config,
    )
}

pub(crate) fn qdrant_config_has_url(config: &QdrantConfig) -> bool {
    config
        .url
        .as_deref()
        .is_some_and(|url| !url.trim().is_empty())
}

fn read_standalone_config() -> Result<Option<StandaloneConfig>, WikiError> {
    let home = gobby_core::gobby_home().map_err(|error| WikiError::Config {
        detail: format!("failed to resolve Gobby home for gwiki indexing config: {error}"),
    })?;
    StandaloneConfig::read_at(&gcore_config_path(&home)).map_err(|error| WikiError::Config {
        detail: format!("failed to read gwiki indexing config: {error}"),
    })
}

fn read_effective_config_layers() -> Result<Option<EffectiveConfigLayers>, WikiError> {
    daemon_mode_layers().map_err(|error| WikiError::Config {
        detail: format!("failed to read daemon effective config for gwiki: {error}"),
    })
}

fn resolve_index_options_from_layers<P: ConfigSource>(
    layers: Option<EffectiveConfigLayers>,
    primary: P,
    standalone: impl FnOnce() -> Result<Option<StandaloneConfig>, WikiError>,
) -> Result<IndexOptions, WikiError> {
    match layers {
        Some((served, routing_overrides)) => {
            let mut source = LayeredConfigSource::new(Some(served), routing_overrides);
            resolve_index_options(&mut source)
        }
        None => {
            let mut source = LayeredConfigSource::new(Some(primary), standalone()?);
            resolve_index_options(&mut source)
        }
    }
}

fn resolve_shared_code_graph_limits_from_layers<P: ConfigSource>(
    layers: Option<EffectiveConfigLayers>,
    primary: P,
    standalone: impl FnOnce() -> Result<Option<StandaloneConfig>, WikiError>,
) -> Result<SharedCodeGraphLimits, WikiError> {
    match layers {
        Some((served, routing_overrides)) => {
            let mut source = LayeredConfigSource::new(Some(served), routing_overrides);
            resolve_shared_code_graph_limits(&mut source)
        }
        None => {
            let mut source = LayeredConfigSource::new(Some(primary), standalone()?);
            resolve_shared_code_graph_limits(&mut source)
        }
    }
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

    use gobby_core::config::{DaemonOrPrimary, DaemonServedConfig, routing_overrides_only};

    use crate::store::MemoryWikiStore;
    use crate::support::test_env::EnvGuard;

    use super::*;

    fn guard_gobby_home(path: &Path) -> EnvGuard {
        EnvGuard::set("GOBBY_HOME", path)
    }

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
    fn shared_code_graph_limits_use_config_source_over_standalone() {
        let primary = TestSource::default()
            .with(SHARED_CODE_CALL_EDGE_LIMIT_KEY, "11")
            .with(SHARED_CODE_IMPORT_EDGE_LIMIT_KEY, "12");
        let fallback = gobby_core::provisioning::StandaloneConfig::from_yaml_str(
            "gwiki:\n  shared_code:\n    call_edge_limit: 21\n    import_edge_limit: 22\n",
        )
        .expect("standalone config");
        let mut source = LayeredConfigSource::new(Some(primary), Some(fallback));

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
    #[serial_test::serial]
    fn local_shared_code_graph_limits_read_gcore_yaml() {
        let home = tempfile::tempdir().expect("home");
        write_file(
            home.path(),
            "gcore.yaml",
            "gwiki:\n  shared_code:\n    call_edge_limit: 31\n    import_edge_limit: 32\n",
        );
        let _guard = guard_gobby_home(home.path());

        let limits = local_shared_code_graph_limits().expect("limits");

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
    #[serial_test::serial]
    fn local_index_options_read_gcore_yaml() {
        let home = tempfile::tempdir().expect("home");
        write_file(
            home.path(),
            "gcore.yaml",
            "indexing:\n  respect_gitignore: false\n",
        );
        let _guard = guard_gobby_home(home.path());

        let options = local_index_options().expect("index options");

        assert!(!options.respect_gitignore);
    }

    #[test]
    fn daemon_index_options_prefer_served_values_over_full_yaml() {
        let served = DaemonServedConfig::new(BTreeMap::from([(
            "indexing.respect_gitignore".to_string(),
            "false".to_string(),
        )]));
        let full_yaml = StandaloneConfig::from_yaml_str_raw(
            "indexing:\n  respect_gitignore: true\nai:\n  routing: direct\n",
        )
        .expect("full yaml");
        let routing = Some(routing_overrides_only(full_yaml));

        let options = resolve_index_options_from_layers(
            Some((served, routing)),
            TestSource::default(),
            || panic!("daemon mode must not read full standalone config"),
        )
        .expect("daemon index options");

        assert!(!options.respect_gitignore);
    }

    #[test]
    fn daemon_index_options_ignore_malformed_full_yaml_fallback() {
        let served = DaemonServedConfig::new(BTreeMap::from([(
            "indexing.respect_gitignore".to_string(),
            "false".to_string(),
        )]));

        let options =
            resolve_index_options_from_layers(Some((served, None)), TestSource::default(), || {
                Err(WikiError::Config {
                    detail: "malformed routing yaml".to_string(),
                })
            })
            .expect("daemon index options");

        assert!(!options.respect_gitignore);
    }

    #[test]
    fn daemon_hub_ai_source_does_not_open_database_connection() {
        let connection_attempted = Cell::new(false);

        let _source = hub_ai_config_source_with(
            "test",
            Box::new(|| {
                connection_attempted.set(true);
                anyhow::bail!("database connection factory must stay lazy")
            }),
            |_primary| {
                Ok(gobby_core::ai_context::AiConfigSource::with_primary(
                    DaemonOrPrimary::Daemon(DaemonServedConfig::new(BTreeMap::new())),
                    None,
                ))
            },
        )
        .expect("daemon AI source");

        assert!(!connection_attempted.get());
    }

    #[test]
    #[serial_test::serial]
    fn memory_indexing_uses_local_index_options() {
        let home = tempfile::tempdir().expect("home");
        write_file(
            home.path(),
            "gcore.yaml",
            "indexing:\n  respect_gitignore: false\n",
        );
        let _guard = guard_gobby_home(home.path());

        let vault = tempfile::tempdir().expect("vault");
        std::fs::create_dir(vault.path().join(".git")).expect("git dir");
        write_file(vault.path(), ".gitignore", "knowledge/topics/ignored.md\n");
        write_file(vault.path(), "knowledge/topics/ignored.md", "# Ignored\n");

        let mut store = MemoryWikiStore::default();
        crate::indexer::index_vault(
            vault.path(),
            &mut store,
            local_index_options().expect("index options"),
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
