use gobby_core::config::{
    ConfigSource, EnvOnlySource, LayeredConfigSource, resolve_indexing_config,
};
use gobby_core::provisioning::{StandaloneConfig, gcore_config_path};
use postgres::Client;

use crate::indexer::IndexOptions;
use crate::{WikiError, indexer};

use super::search::PostgresConfigSource;

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
    let standalone = read_standalone_config()?;
    let mut source = LayeredConfigSource::new(Some(EnvOnlySource), standalone);
    resolve_index_options(&mut source)
}

pub(crate) fn index_options_from_conn(conn: &mut Client) -> Result<IndexOptions, WikiError> {
    let standalone = read_standalone_config()?;
    let primary = PostgresConfigSource { conn };
    let mut source = LayeredConfigSource::new(Some(primary), standalone);
    resolve_index_options(&mut source)
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
    let standalone = read_standalone_config()?;
    let primary = PostgresConfigSource { conn };
    let mut source = LayeredConfigSource::new(Some(primary), standalone);
    resolve_shared_code_graph_limits(&mut source)
}

fn read_standalone_config() -> Result<Option<StandaloneConfig>, WikiError> {
    let home = gobby_core::gobby_home().map_err(|error| WikiError::Config {
        detail: format!("failed to resolve Gobby home for gwiki indexing config: {error}"),
    })?;
    StandaloneConfig::read_at(&gcore_config_path(&home)).map_err(|error| WikiError::Config {
        detail: format!("failed to read gwiki indexing config: {error}"),
    })
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
    use std::collections::BTreeMap;
    use std::path::Path;
    use std::sync::{Mutex, MutexGuard};

    use crate::store::MemoryWikiStore;

    use super::*;

    static ENV_LOCK: Mutex<()> = Mutex::new(());

    struct EnvGuard {
        _lock: MutexGuard<'static, ()>,
        previous: Option<std::ffi::OsString>,
    }

    impl EnvGuard {
        fn set_gobby_home(path: &Path) -> Self {
            let lock = ENV_LOCK
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            let previous = std::env::var_os("GOBBY_HOME");
            // SAFETY: ENV_LOCK serializes test-only process environment mutation.
            unsafe { std::env::set_var("GOBBY_HOME", path) };
            Self {
                _lock: lock,
                previous,
            }
        }
    }

    impl Drop for EnvGuard {
        fn drop(&mut self) {
            match self.previous.as_ref() {
                // SAFETY: ENV_LOCK serializes test-only process environment restoration.
                Some(value) => unsafe { std::env::set_var("GOBBY_HOME", value) },
                // SAFETY: ENV_LOCK serializes test-only process environment restoration.
                None => unsafe { std::env::remove_var("GOBBY_HOME") },
            }
        }
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
        let _guard = EnvGuard::set_gobby_home(home.path());

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
        let _guard = EnvGuard::set_gobby_home(home.path());

        let options = local_index_options().expect("index options");

        assert!(!options.respect_gitignore);
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
        let _guard = EnvGuard::set_gobby_home(home.path());

        let vault = tempfile::tempdir().expect("vault");
        std::fs::create_dir(vault.path().join(".git")).expect("git dir");
        write_file(vault.path(), ".gitignore", "knowledge/topics/ignored.md\n");
        write_file(vault.path(), "knowledge/topics/ignored.md", "# Ignored\n");

        let mut store = MemoryWikiStore::default();
        crate::indexer::index_vault_with_options(
            vault.path(),
            &mut store,
            local_index_options().expect("index options"),
        )
        .expect("index vault");

        assert!(
            store
                .documents
                .contains_key(&std::path::PathBuf::from("knowledge/topics/ignored.md"))
        );
    }
}
