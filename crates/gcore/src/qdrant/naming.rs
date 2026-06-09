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

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum CollectionNameError {
    #[error("Qdrant collection name cannot be empty")]
    Empty,
    #[error("Qdrant collection name `{name}` is reserved")]
    Reserved { name: String },
    #[error("Qdrant collection name `{name}` contains invalid character `{character}`")]
    InvalidCharacter { name: String, character: char },
    #[error("Qdrant collection name `{name}` has leading or trailing whitespace")]
    SurroundingWhitespace { name: String },
}

/// Build the preferred collection name from namespace and scope.
pub fn collection_name(
    namespace: &str,
    scope: CollectionScope<'_>,
) -> Result<String, CollectionNameError> {
    match scope {
        CollectionScope::Project(id) => {
            validate_collection_name_component(id)?;
            Ok(format!("{namespace}_project_{id}"))
        }
        CollectionScope::Topic(name) => {
            validate_collection_name_component(name)?;
            Ok(format!("{namespace}_topic_{name}"))
        }
        CollectionScope::Custom(name) => {
            validate_collection_name_component(name)?;
            Ok(name.to_string())
        }
    }
}

fn validate_collection_name_component(name: &str) -> Result<(), CollectionNameError> {
    if name.is_empty() {
        return Err(CollectionNameError::Empty);
    }
    if name.trim() != name {
        return Err(CollectionNameError::SurroundingWhitespace {
            name: name.to_string(),
        });
    }
    if matches!(name, "." | "..") {
        return Err(CollectionNameError::Reserved {
            name: name.to_string(),
        });
    }
    if let Some(character) = name.chars().find(|character| {
        character.is_ascii_control()
            || character.is_ascii_whitespace()
            || matches!(character, '/' | '\\' | ':')
    }) {
        return Err(CollectionNameError::InvalidCharacter {
            name: name.to_string(),
            character,
        });
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn collection_name_covers_all_scopes() {
        assert_eq!(
            collection_name("gwiki", CollectionScope::Project("abc-123")).unwrap(),
            "gwiki_project_abc-123"
        );
        assert_eq!(
            collection_name("gwiki", CollectionScope::Topic("rust-async")).unwrap(),
            "gwiki_topic_rust-async"
        );
        assert_eq!(
            collection_name("gcode", CollectionScope::Custom("code_symbols_abc-123")).unwrap(),
            "code_symbols_abc-123"
        );
    }

    #[test]
    fn custom_collection_name_rejects_path_like_and_blank_names() {
        for invalid in [
            "",
            " ",
            ".",
            "..",
            "bad/name",
            r"bad\name",
            "bad:name",
            "bad name",
            "bad\nname",
        ] {
            assert!(
                collection_name("gcode", CollectionScope::Custom(invalid)).is_err(),
                "{invalid:?} should fail"
            );
        }
    }

    #[test]
    fn scoped_collection_names_reject_invalid_components() {
        for invalid in ["", "bad/name", "bad:name", "bad name"] {
            assert!(
                collection_name("gwiki", CollectionScope::Project(invalid)).is_err(),
                "project id {invalid:?} should fail"
            );
            assert!(
                collection_name("gwiki", CollectionScope::Topic(invalid)).is_err(),
                "topic {invalid:?} should fail"
            );
        }
    }
}
