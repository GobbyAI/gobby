use gobby_core::config::{EnvOnlySource, LayeredConfigSource, resolve_indexing_config};
use gobby_core::provisioning::{StandaloneConfig, gcore_config_path};
use postgres::Client;

use crate::indexer::IndexOptions;
use crate::{WikiError, indexer};

use super::search::PostgresConfigSource;

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

#[cfg(test)]
mod tests {
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
