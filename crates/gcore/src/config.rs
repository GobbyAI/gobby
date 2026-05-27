//! Shared configuration-resolution boundary.
//!
//! This module is the public home for lightweight configuration contracts that
//! are shared across Gobby Rust crates. Concrete service resolution is added in
//! focused follow-up modules so this baseline crate remains small.

/// FalkorDB connection configuration.
///
/// Graph name selection is consumer-owned.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FalkorConfig {
    pub host: String,
    pub port: u16,
    pub password: Option<String>,
}

/// Qdrant connection configuration.
///
/// Collection naming is consumer-owned.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct QdrantConfig {
    pub url: Option<String>,
    pub api_key: Option<String>,
}

/// Embedding API configuration for an OpenAI-compatible endpoint.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EmbeddingConfig {
    pub api_base: String,
    pub model: String,
    pub api_key: Option<String>,
}

const FALKORDB_DEFAULT_PORT: u16 = 16379;
const EMBEDDING_DEFAULT_MODEL: &str = "nomic-embed-text";

#[cfg(test)]
pub(crate) static TEST_ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

/// Decode a config_store value from its stored representation.
pub fn decode_config_value(raw: &str) -> Option<String> {
    match serde_json::from_str::<serde_json::Value>(raw) {
        Ok(serde_json::Value::String(value)) => Some(value),
        Ok(value @ (serde_json::Value::Array(_) | serde_json::Value::Object(_))) => {
            Some(serde_json::to_string(&value).unwrap_or_else(|_| raw.to_string()))
        }
        Ok(serde_json::Value::Null) => None,
        Ok(value) => Some(value.to_string()),
        Err(_) => Some(raw.to_string()),
    }
}

/// Resolve `${VAR}` and `${VAR:-default}` environment variable patterns.
pub fn resolve_env_pattern(value: &str) -> anyhow::Result<Option<String>> {
    if !value.contains("${") {
        return Ok(Some(value.to_string()));
    }

    let mut output = String::with_capacity(value.len());
    let mut rest = value;
    let mut unresolved = false;

    while let Some(start) = rest.find("${") {
        output.push_str(&rest[..start]);
        let pattern = &rest[start + 2..];
        let Some(end) = pattern.find('}') else {
            anyhow::bail!("unterminated environment pattern in `{value}`");
        };

        let expression = &pattern[..end];
        if expression.is_empty() {
            anyhow::bail!("empty environment pattern in `{value}`");
        }

        let (name, default) = match expression.split_once(":-") {
            Some((name, default)) => (name, Some(default)),
            None => (expression, None),
        };
        if name.is_empty() {
            anyhow::bail!("empty environment variable name in `{value}`");
        }

        match std::env::var(name) {
            Ok(current) if !(current.is_empty() && default.is_some()) => {
                output.push_str(&current);
            }
            Ok(_) | Err(std::env::VarError::NotPresent) => match default {
                Some(default) => output.push_str(default),
                None => unresolved = true,
            },
            Err(std::env::VarError::NotUnicode(_)) => {
                anyhow::bail!("environment variable `{name}` is not valid unicode");
            }
        }

        rest = &pattern[end + 1..];
    }

    output.push_str(rest);
    if unresolved {
        Ok(None)
    } else {
        Ok(Some(output))
    }
}

/// Source for config values and interpolation.
pub trait ConfigSource {
    /// Read a decoded config value by key.
    fn config_value(&mut self, key: &str) -> Option<String>;

    /// Resolve interpolation patterns in a config value.
    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String>;
}

/// Environment-only source for consumers without database access.
pub struct EnvOnlySource;

impl ConfigSource for EnvOnlySource {
    fn config_value(&mut self, _key: &str) -> Option<String> {
        None
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        resolve_env_pattern(value)?.ok_or_else(|| anyhow::anyhow!("unresolved pattern: {value}"))
    }
}

/// Resolve FalkorDB config from env, config_store, then defaults.
pub fn resolve_falkordb_config(source: &mut impl ConfigSource) -> Option<FalkorConfig> {
    let host = resolve_setting(source, "GOBBY_FALKORDB_HOST", "databases.falkordb.host")?;
    let port = resolve_port(
        source,
        "GOBBY_FALKORDB_PORT",
        "databases.falkordb.port",
        FALKORDB_DEFAULT_PORT,
    );
    let password = resolve_setting(
        source,
        "GOBBY_FALKORDB_PASSWORD",
        "databases.falkordb.requirepass",
    );

    Some(FalkorConfig {
        host,
        port,
        password,
    })
}

/// Resolve Qdrant config from env and config_store.
pub fn resolve_qdrant_config(source: &mut impl ConfigSource) -> Option<QdrantConfig> {
    let url = resolve_setting(source, "GOBBY_QDRANT_URL", "databases.qdrant.url");
    url.as_ref()?;
    let api_key = resolve_setting(source, "GOBBY_QDRANT_API_KEY", "databases.qdrant.api_key");

    Some(QdrantConfig { url, api_key })
}

/// Resolve embedding API config from env, config_store, then defaults.
pub fn resolve_embedding_config(source: &mut impl ConfigSource) -> Option<EmbeddingConfig> {
    let api_base = resolve_setting(source, "GOBBY_EMBEDDING_URL", "embeddings.api_base")?;
    let model = resolve_setting(source, "GOBBY_EMBEDDING_MODEL", "embeddings.model")
        .unwrap_or_else(|| EMBEDDING_DEFAULT_MODEL.to_string());
    let api_key = resolve_setting(source, "GOBBY_EMBEDDING_API_KEY", "embeddings.api_key");

    Some(EmbeddingConfig {
        api_base,
        model,
        api_key,
    })
}

fn resolve_setting(
    source: &mut impl ConfigSource,
    env_key: &str,
    config_key: &str,
) -> Option<String> {
    let value = env_value(env_key).or_else(|| source.config_value(config_key))?;
    resolve_non_empty(source, &value)
}

fn resolve_port(
    source: &mut impl ConfigSource,
    env_key: &str,
    config_key: &str,
    default: u16,
) -> u16 {
    let Some(raw_port) = env_value(env_key).or_else(|| source.config_value(config_key)) else {
        return default;
    };
    let Some(resolved) = resolve_non_empty(source, &raw_port) else {
        return default;
    };
    resolved.parse::<u16>().unwrap_or(default)
}

fn resolve_non_empty(source: &mut impl ConfigSource, value: &str) -> Option<String> {
    if value.trim().is_empty() {
        return None;
    }
    source
        .resolve_value(value)
        .ok()
        .filter(|resolved| !resolved.trim().is_empty())
}

fn env_value(key: &str) -> Option<String> {
    std::env::var(key)
        .ok()
        .filter(|value| !value.trim().is_empty())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
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
                "GOBBY_TEST_PRESENT",
                "GOBBY_TEST_MISSING",
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

    #[derive(Default)]
    struct TestSource {
        values: HashMap<&'static str, String>,
        resolved_values: Vec<String>,
    }

    impl TestSource {
        fn with_values(values: impl IntoIterator<Item = (&'static str, &'static str)>) -> Self {
            Self {
                values: values
                    .into_iter()
                    .map(|(key, value)| (key, value.to_string()))
                    .collect(),
                resolved_values: Vec::new(),
            }
        }

        fn with_raw_values(values: impl IntoIterator<Item = (&'static str, &'static str)>) -> Self {
            Self {
                values: values
                    .into_iter()
                    .filter_map(|(key, value)| decode_config_value(value).map(|v| (key, v)))
                    .collect(),
                resolved_values: Vec::new(),
            }
        }
    }

    impl ConfigSource for TestSource {
        fn config_value(&mut self, key: &str) -> Option<String> {
            self.values.get(key).cloned()
        }

        fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
            self.resolved_values.push(value.to_string());
            if let Some(secret_name) = value.strip_prefix("$secret:") {
                return Ok(format!("resolved-{secret_name}"));
            }
            Ok(resolve_env_pattern(value)?.unwrap_or_else(|| value.to_string()))
        }
    }

    #[test]
    fn decode_config_value_handles_json_and_plain() {
        assert_eq!(
            decode_config_value("\"http://host:7474\""),
            Some("http://host:7474".to_string())
        );
        assert_eq!(
            decode_config_value(r#"["alpha",1,true]"#),
            Some(r#"["alpha",1,true]"#.to_string())
        );
        assert_eq!(
            decode_config_value(r#"{"host":"falkor.local","port":16379}"#),
            Some(r#"{"host":"falkor.local","port":16379}"#.to_string())
        );
        assert_eq!(decode_config_value("42"), Some("42".to_string()));
        assert_eq!(decode_config_value("true"), Some("true".to_string()));
        assert_eq!(
            decode_config_value("http://plain:7474"),
            Some("http://plain:7474".to_string())
        );
        assert_eq!(decode_config_value("null"), None);
    }

    #[test]
    fn resolve_env_pattern_with_defaults() {
        let env = EnvGuard::new();
        env.set("GOBBY_TEST_PRESENT", "present-value");

        assert_eq!(
            resolve_env_pattern("${GOBBY_TEST_PRESENT}").unwrap(),
            Some("present-value".to_string())
        );
        assert_eq!(
            resolve_env_pattern("prefix-${GOBBY_TEST_PRESENT}-suffix").unwrap(),
            Some("prefix-present-value-suffix".to_string())
        );
        assert_eq!(
            resolve_env_pattern("${GOBBY_TEST_MISSING:-fallback}").unwrap(),
            Some("fallback".to_string())
        );
        assert_eq!(resolve_env_pattern("${GOBBY_TEST_MISSING}").unwrap(), None);
        assert_eq!(
            resolve_env_pattern("plain-value").unwrap(),
            Some("plain-value".to_string())
        );
    }

    #[test]
    fn env_overrides_config_store() {
        let env = EnvGuard::new();
        env.set("GOBBY_FALKORDB_HOST", "env-falkor.local");
        env.set("GOBBY_FALKORDB_PORT", "17000");
        env.set("GOBBY_FALKORDB_PASSWORD", "env-pass");
        env.set("GOBBY_QDRANT_URL", "http://env-qdrant:6333");
        env.set("GOBBY_QDRANT_API_KEY", "env-qdrant-key");

        let mut source = TestSource::with_values([
            ("databases.falkordb.host", "stored-falkor.local"),
            ("databases.falkordb.port", "16000"),
            ("databases.falkordb.requirepass", "stored-pass"),
            ("databases.qdrant.url", "http://stored-qdrant:6333"),
            ("databases.qdrant.api_key", "stored-qdrant-key"),
        ]);

        let falkordb = resolve_falkordb_config(&mut source).expect("falkordb config");
        let qdrant = resolve_qdrant_config(&mut source).expect("qdrant config");

        assert_eq!(falkordb.host, "env-falkor.local");
        assert_eq!(falkordb.port, 17000);
        assert_eq!(falkordb.password.as_deref(), Some("env-pass"));
        assert_eq!(qdrant.url.as_deref(), Some("http://env-qdrant:6333"));
        assert_eq!(qdrant.api_key.as_deref(), Some("env-qdrant-key"));
    }

    #[test]
    fn config_source_handles_secrets() {
        let _env = EnvGuard::new();
        let mut source = TestSource::with_values([
            ("databases.falkordb.host", "falkor.local"),
            ("databases.falkordb.requirepass", "$secret:FALKOR_PASS"),
        ]);

        let config = resolve_falkordb_config(&mut source).expect("falkordb config");

        assert_eq!(config.password.as_deref(), Some("resolved-FALKOR_PASS"));
        assert!(
            source
                .resolved_values
                .iter()
                .any(|value| value == "$secret:FALKOR_PASS")
        );
    }

    #[test]
    fn embedding_url_env_var_is_canonical() {
        let env = EnvGuard::new();
        env.set("GOBBY_EMBEDDING_URL", "http://env-embedding:11434");

        let mut source = TestSource::with_values([
            ("embeddings.api_base", "http://stored-embedding:11434"),
            ("embeddings.model", "stored-model"),
        ]);

        let config = resolve_embedding_config(&mut source).expect("embedding config");

        assert_eq!(config.api_base, "http://env-embedding:11434");
        assert_eq!(config.model, "stored-model");
    }

    #[test]
    fn postgres_config_source_resolves_secrets() {
        let _env = EnvGuard::new();

        struct ConnectionLike {
            values: HashMap<&'static str, String>,
            secret_reads: usize,
        }

        struct PostgresConfigSource<'a> {
            conn: &'a mut ConnectionLike,
        }

        impl ConfigSource for PostgresConfigSource<'_> {
            fn config_value(&mut self, key: &str) -> Option<String> {
                self.conn.values.get(key).cloned()
            }

            fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
                self.conn.secret_reads += 1;
                Ok(format!("secret::{value}"))
            }
        }

        let mut conn = ConnectionLike {
            values: HashMap::from([
                (
                    "embeddings.api_base",
                    "http://stored-embedding:11434".to_string(),
                ),
                ("embeddings.api_key", "$secret:OPENAI_API_KEY".to_string()),
            ]),
            secret_reads: 0,
        };
        let config = {
            let mut source = PostgresConfigSource { conn: &mut conn };
            resolve_embedding_config(&mut source).expect("embedding config")
        };

        assert_eq!(
            config.api_key.as_deref(),
            Some("secret::$secret:OPENAI_API_KEY")
        );
        assert_eq!(conn.secret_reads, 2);
    }

    #[test]
    fn resolve_config_handles_json_encoded_store_values() {
        let _env = EnvGuard::new();
        let mut source = TestSource::with_raw_values([
            ("databases.falkordb.host", r#""json-falkor.local""#),
            ("databases.falkordb.port", r#""17001""#),
            ("databases.falkordb.requirepass", r#""$secret:FALKOR_PASS""#),
            ("databases.qdrant.url", r#""http://json-qdrant:6333""#),
            ("databases.qdrant.api_key", r#"["alpha",1]"#),
            ("embeddings.api_base", r#""http://json-embedding:11434""#),
            ("embeddings.model", r#"["model",1]"#),
        ]);

        let falkordb = resolve_falkordb_config(&mut source).expect("falkordb config");
        let qdrant = resolve_qdrant_config(&mut source).expect("qdrant config");
        let embedding = resolve_embedding_config(&mut source).expect("embedding config");

        assert_eq!(falkordb.host, "json-falkor.local");
        assert_eq!(falkordb.port, 17001);
        assert_eq!(falkordb.password.as_deref(), Some("resolved-FALKOR_PASS"));
        assert_eq!(qdrant.url.as_deref(), Some("http://json-qdrant:6333"));
        assert_eq!(qdrant.api_key.as_deref(), Some(r#"["alpha",1]"#));
        assert_eq!(embedding.api_base, "http://json-embedding:11434");
        assert_eq!(embedding.model, r#"["model",1]"#);
    }

    #[test]
    fn falkordb_config_has_no_domain_graph_name() {
        let config = FalkorConfig {
            host: "falkor.local".to_string(),
            port: 16379,
            password: None,
        };

        assert!(!format!("{config:?}").contains("graph"));
        let forbidden = ["gobby", "_", "code"].concat();
        assert!(!include_str!("config.rs").contains(&forbidden));
    }

    #[test]
    fn qdrant_config_has_no_domain_collection_prefix() {
        let config = QdrantConfig {
            url: Some("http://qdrant:6333".to_string()),
            api_key: None,
        };

        assert!(!format!("{config:?}").contains("collection"));
    }
}
