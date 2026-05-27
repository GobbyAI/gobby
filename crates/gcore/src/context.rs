//! Shared runtime context boundary.
//!
//! Consumer crates keep their CLI flags and domain state locally. This module
//! owns the public location for cross-crate project, daemon, and service context
//! types as the Rust foundation expands.

use std::path::PathBuf;

use crate::config::{
    ConfigSource, EmbeddingConfig, FalkorConfig, QdrantConfig, resolve_embedding_config,
    resolve_falkordb_config, resolve_qdrant_config,
};

/// Resolved runtime context for any gobby-core consumer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CoreContext {
    /// Project root directory containing `.gobby/`.
    pub project_root: PathBuf,
    /// Project ID from `.gobby/project.json`.
    pub project_id: String,
    /// PostgreSQL hub DSN resolved by the consumer.
    pub database_url: Option<String>,
    /// FalkorDB config when available.
    pub falkordb: Option<FalkorConfig>,
    /// Qdrant config when available.
    pub qdrant: Option<QdrantConfig>,
    /// Embedding API config when available.
    pub embedding: Option<EmbeddingConfig>,
    /// Gobby daemon base URL.
    pub daemon_url: Option<String>,
}

impl CoreContext {
    /// Build a context from pre-resolved project identity and DSN inputs.
    pub fn build(
        project_root: PathBuf,
        project_id: String,
        database_url: Option<String>,
        source: &mut impl ConfigSource,
    ) -> Self {
        let falkordb = resolve_falkordb_config(source);
        let qdrant = resolve_qdrant_config(source);
        let embedding = resolve_embedding_config(source);
        let daemon_url = Some(crate::daemon_url::daemon_url());

        Self {
            project_root,
            project_id,
            database_url,
            falkordb,
            qdrant,
            embedding,
            daemon_url,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{EnvOnlySource, TEST_ENV_LOCK};
    use std::sync::MutexGuard;

    struct EnvGuard {
        _lock: MutexGuard<'static, ()>,
    }

    impl EnvGuard {
        fn new() -> Self {
            let guard = Self {
                _lock: TEST_ENV_LOCK
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner()),
            };
            guard.clear();
            guard
        }

        fn clear(&self) {
            for key in [
                "GOBBY_FALKORDB_HOST",
                "GOBBY_FALKORDB_PORT",
                "GOBBY_FALKORDB_PASSWORD",
                "GOBBY_QDRANT_URL",
                "GOBBY_QDRANT_API_KEY",
                "GOBBY_EMBEDDING_URL",
                "GOBBY_EMBEDDING_MODEL",
                "GOBBY_EMBEDDING_API_KEY",
            ] {
                unsafe { std::env::remove_var(key) };
            }
        }

        fn set(&self, key: &str, value: &str) {
            unsafe { std::env::set_var(key, value) };
        }
    }

    impl Drop for EnvGuard {
        fn drop(&mut self) {
            self.clear();
        }
    }

    #[test]
    fn missing_optional_services_are_none() {
        let _env = EnvGuard::new();
        let mut source = EnvOnlySource;
        let root = std::path::PathBuf::from("/tmp/gobby-project");

        let context = CoreContext::build(root.clone(), "project-id".to_string(), None, &mut source);

        assert_eq!(context.project_root, root);
        assert_eq!(context.project_id, "project-id");
        assert_eq!(context.database_url, None);
        assert!(context.falkordb.is_none());
        assert!(context.qdrant.is_none());
        assert!(context.embedding.is_none());
        assert!(context.daemon_url.is_some());
    }

    #[test]
    fn build_with_env_only_source() {
        let env = EnvGuard::new();
        env.set("GOBBY_FALKORDB_HOST", "env-falkor.local");
        env.set("GOBBY_FALKORDB_PORT", "17000");
        env.set("GOBBY_QDRANT_URL", "http://env-qdrant:6333");
        env.set("GOBBY_EMBEDDING_URL", "http://env-embedding:11434");
        env.set("GOBBY_EMBEDDING_MODEL", "env-model");

        let mut source = EnvOnlySource;
        let root = std::path::PathBuf::from("/tmp/gobby-project");

        let context = CoreContext::build(
            root.clone(),
            "project-id".to_string(),
            Some("postgres://example".to_string()),
            &mut source,
        );

        assert_eq!(context.project_root, root);
        assert_eq!(context.project_id, "project-id");
        assert_eq!(context.database_url.as_deref(), Some("postgres://example"));
        assert_eq!(
            context.falkordb.as_ref().map(|c| c.host.as_str()),
            Some("env-falkor.local")
        );
        assert_eq!(
            context.qdrant.as_ref().and_then(|c| c.url.as_deref()),
            Some("http://env-qdrant:6333")
        );
        assert_eq!(
            context.embedding.as_ref().map(|c| c.api_base.as_str()),
            Some("http://env-embedding:11434")
        );
        assert_eq!(
            context.embedding.as_ref().map(|c| c.model.as_str()),
            Some("env-model")
        );
        assert!(context.daemon_url.is_some());
    }
}
