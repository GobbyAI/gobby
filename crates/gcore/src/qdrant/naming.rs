use crate::config::QdrantConfig;

/// Scope for a Qdrant collection, allowing caller-controlled naming.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CollectionScope<'a> {
    /// `{namespace}_project_{id}` - per-project vector store.
    Project(&'a str),
    /// `{namespace}_topic_{name}` - topic-scoped store.
    Topic(&'a str),
    /// Verbatim collection name, without namespace prefixing.
    Custom(&'a str),
}

/// Build the preferred collection name from namespace and scope.
pub fn collection_name(namespace: &str, scope: CollectionScope<'_>) -> String {
    match scope {
        CollectionScope::Project(id) => format!("{namespace}_project_{id}"),
        CollectionScope::Topic(name) => format!("{namespace}_topic_{name}"),
        CollectionScope::Custom(name) => name.to_string(),
    }
}

/// Build the deprecated collection name used before underscore-separated names.
pub fn legacy_collection_name(namespace: &str, scope: CollectionScope<'_>) -> String {
    match scope {
        CollectionScope::Project(id) => format!("{namespace}:project:{id}"),
        CollectionScope::Topic(name) => format!("{namespace}:topic:{name}"),
        CollectionScope::Custom(name) => name.to_string(),
    }
}

/// Resolve the collection to use, preferring current names and falling back to
/// an existing legacy collection when the current collection has not been built.
pub fn resolve_collection_name(
    config: &QdrantConfig,
    namespace: &str,
    scope: CollectionScope<'_>,
) -> anyhow::Result<String> {
    let preferred = collection_name(namespace, scope);
    let legacy = legacy_collection_name(namespace, scope);
    if preferred == legacy {
        return Ok(preferred);
    }

    if super::collection_schema(config, &preferred)?.is_some() {
        return Ok(preferred);
    }
    if super::collection_schema(config, &legacy)?.is_some() {
        return Ok(legacy);
    }
    Ok(preferred)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn collection_name_covers_all_scopes() {
        assert_eq!(
            collection_name("gwiki", CollectionScope::Project("abc-123")),
            "gwiki_project_abc-123"
        );
        assert_eq!(
            collection_name("gwiki", CollectionScope::Topic("rust-async")),
            "gwiki_topic_rust-async"
        );
        assert_eq!(
            collection_name("gcode", CollectionScope::Custom("code_symbols_abc-123")),
            "code_symbols_abc-123"
        );
    }

    #[test]
    fn legacy_collection_name_covers_named_scopes() {
        assert_eq!(
            legacy_collection_name("gwiki", CollectionScope::Project("abc-123")),
            "gwiki:project:abc-123"
        );
        assert_eq!(
            legacy_collection_name("gwiki", CollectionScope::Topic("rust-async")),
            "gwiki:topic:rust-async"
        );
        assert_eq!(
            legacy_collection_name("ignored", CollectionScope::Custom("code_symbols_project-1")),
            "code_symbols_project-1"
        );
    }
}
