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
}
