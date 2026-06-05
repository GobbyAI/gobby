use super::*;

const MAX_YAML_FLATTEN_DEPTH: usize = 64;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EmbeddingBootstrap {
    pub provider: String,
    pub api_base: String,
    pub model: String,
    pub vector_dim: usize,
    pub query_prefix: Option<String>,
    pub api_key: Option<String>,
}

impl EmbeddingBootstrap {
    pub fn lm_studio() -> Self {
        Self {
            provider: "lm-studio".to_string(),
            api_base: DEFAULT_LM_STUDIO_API_BASE.to_string(),
            model: DEFAULT_LM_STUDIO_MODEL.to_string(),
            vector_dim: DEFAULT_EMBEDDING_VECTOR_DIM,
            query_prefix: None,
            api_key: None,
        }
    }

    pub fn ollama() -> Self {
        Self {
            provider: "ollama".to_string(),
            api_base: DEFAULT_OLLAMA_API_BASE.to_string(),
            model: DEFAULT_OLLAMA_MODEL.to_string(),
            vector_dim: DEFAULT_EMBEDDING_VECTOR_DIM,
            query_prefix: None,
            api_key: None,
        }
    }
}

pub fn write_standalone_bootstrap(
    path: &Path,
    database_url: &str,
    options: &DockerServiceOptions,
    compose_file: Option<&Path>,
    embedding: Option<&EmbeddingBootstrap>,
) -> anyhow::Result<StandaloneConfig> {
    let mut config = StandaloneConfig::empty();
    config.set("databases.postgres.dsn", database_url);
    config.set("databases.falkordb.host", &options.falkordb_host);
    config.set("databases.falkordb.port", options.falkordb_port.to_string());
    config.set("databases.falkordb.password", &options.falkordb_password);
    config.set("databases.qdrant.url", options.qdrant_url());
    if let Some(embedding) = embedding {
        remove_legacy_embedding_keys(&mut config);
        config.set(embedding_keys::AI_PROVIDER, &embedding.provider);
        config.set(embedding_keys::AI_API_BASE, &embedding.api_base);
        config.set(embedding_keys::AI_MODEL, &embedding.model);
        config.set(embedding_keys::AI_DIM, embedding.vector_dim.to_string());
        if let Some(query_prefix) = &embedding.query_prefix {
            config.set(embedding_keys::AI_QUERY_PREFIX, query_prefix);
        }
        if let Some(api_key) = &embedding.api_key {
            config.set(embedding_keys::AI_API_KEY, api_key);
        }
    }
    if let Some(compose_file) = compose_file {
        config.set("services.compose_file", compose_file.display().to_string());
    }
    config.write_at(path)?;
    Ok(config)
}

fn remove_legacy_embedding_keys(config: &mut StandaloneConfig) {
    let legacy_keys = embedding_keys::legacy_keys();
    let removed = legacy_keys
        .iter()
        .filter(|key| config.get(key).is_some())
        .cloned()
        .collect::<Vec<_>>();
    if !removed.is_empty() {
        log::warn!(
            "removing legacy embedding config keys [{}]; embedding config now lives under \
             ai.embeddings.* and unsupported legacy values are dropped. See \
             hub-install-contract.md and ai-daemon-contract.md for migration guidance.",
            removed.join(", ")
        );
    }
    for key in legacy_keys {
        config.remove(&key);
    }
}

pub(crate) fn flatten_yaml_value(
    prefix: Option<&str>,
    value: &serde_yaml::Value,
    output: &mut BTreeMap<String, String>,
) -> anyhow::Result<()> {
    flatten_yaml_value_at_depth(prefix, value, output, 0)
}

fn flatten_yaml_value_at_depth(
    prefix: Option<&str>,
    value: &serde_yaml::Value,
    output: &mut BTreeMap<String, String>,
    depth: usize,
) -> anyhow::Result<()> {
    if depth > MAX_YAML_FLATTEN_DEPTH {
        anyhow::bail!(
            "gcore.yaml path `{}` exceeds maximum depth of {MAX_YAML_FLATTEN_DEPTH}",
            yaml_path(prefix)
        );
    }
    match value {
        serde_yaml::Value::Null => Ok(()),
        serde_yaml::Value::Mapping(mapping) => {
            for (key, value) in mapping {
                let Some(key) = key.as_str() else {
                    anyhow::bail!(
                        "gcore.yaml path `{}` has a non-string key",
                        yaml_path(prefix)
                    );
                };
                let joined = match prefix {
                    Some(prefix) if !prefix.is_empty() => format!("{prefix}.{key}"),
                    _ => key.to_string(),
                };
                match value {
                    serde_yaml::Value::Mapping(_) => {
                        // Dotted keys may be flattened prefixes such as
                        // `ai.embeddings`, so nested values still need recursion.
                        flatten_yaml_value_at_depth(Some(&joined), value, output, depth + 1)?;
                    }
                    _ => {
                        if let Some(text) = scalar_to_string(&joined, value)? {
                            output.insert(joined, text);
                        }
                    }
                }
            }
            Ok(())
        }
        _ => {
            let Some(prefix) = prefix else {
                anyhow::bail!("gcore.yaml path `<root>` must be a mapping");
            };
            if let Some(text) = scalar_to_string(prefix, value)? {
                output.insert(prefix.to_string(), text);
            }
            Ok(())
        }
    }
}

fn scalar_to_string(path: &str, value: &serde_yaml::Value) -> anyhow::Result<Option<String>> {
    Ok(match value {
        serde_yaml::Value::Null => None,
        serde_yaml::Value::String(value) => Some(value.clone()),
        serde_yaml::Value::Bool(value) => Some(value.to_string()),
        serde_yaml::Value::Number(value) => Some(value.to_string()),
        serde_yaml::Value::Sequence(_) => {
            anyhow::bail!("gcore.yaml path `{path}` cannot be a sequence")
        }
        serde_yaml::Value::Mapping(_) => {
            anyhow::bail!("gcore.yaml path `{path}` cannot be a mapping")
        }
        // Tagged values may wrap scalars or collections; preserve serde_yaml's
        // spelling instead of inventing a lossy custom representation.
        other => Some(
            serde_yaml::to_string(other)
                .with_context(|| format!("convert gcore.yaml path `{path}` to string"))?
                .trim()
                .to_string(),
        ),
    })
}

fn yaml_path(prefix: Option<&str>) -> &str {
    prefix.filter(|path| !path.is_empty()).unwrap_or("<root>")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn flatten(contents: &str) -> anyhow::Result<BTreeMap<String, String>> {
        let value = serde_yaml::from_str(contents)?;
        let mut output = BTreeMap::new();
        flatten_yaml_value(None, &value, &mut output)?;
        Ok(output)
    }

    #[test]
    fn flatten_yaml_errors_include_root_path() {
        let error = flatten("true").expect_err("root scalar must fail");

        assert!(error.to_string().contains("`<root>`"));
    }

    #[test]
    fn flatten_yaml_errors_include_mapping_path_for_non_string_keys() {
        let error = flatten("ai:\n  ? [bad]\n  : value\n").expect_err("non-string key must fail");

        assert!(error.to_string().contains("`ai`"));
    }

    #[test]
    fn flatten_yaml_errors_include_scalar_path() {
        let error = flatten("ai:\n  embeddings:\n    provider:\n      - openai\n")
            .expect_err("sequence scalar must fail");

        assert!(error.to_string().contains("`ai.embeddings.provider`"));
    }

    #[test]
    fn flatten_yaml_depth_errors_include_current_path() {
        let mut contents = String::new();
        for index in 0..=MAX_YAML_FLATTEN_DEPTH + 1 {
            contents.push_str(&"  ".repeat(index));
            contents.push_str(&format!("k{index}:\n"));
        }

        let error = flatten(&contents).expect_err("too-deep nesting must fail");

        assert!(error.to_string().contains("k0.k1"));
    }
}
