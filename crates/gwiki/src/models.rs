use serde::{Deserialize, Serialize};
use serde_json::Value;

pub const WIKI_DOC_LABEL: &str = "WikiDoc";
pub const WIKI_SOURCE_LABEL: &str = "WikiSource";
pub const WIKI_TOPIC_LABEL: &str = "WikiTopic";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum WikiScope {
    Project { project_id: String },
    Topic { name: String },
}

impl WikiScope {
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Project { .. } => "project",
            Self::Topic { .. } => "topic",
        }
    }

    pub fn identity(&self) -> &str {
        match self {
            Self::Project { project_id } => project_id,
            Self::Topic { name } => name,
        }
    }

    pub fn project_id(&self) -> Option<&str> {
        match self {
            Self::Project { project_id } => Some(project_id),
            Self::Topic { .. } => None,
        }
    }

    pub fn topic_name(&self) -> Option<&str> {
        match self {
            Self::Project { .. } => None,
            Self::Topic { name } => Some(name),
        }
    }

    pub fn vector_collection_name(&self) -> String {
        match self {
            Self::Project { project_id } => project_collection_name(project_id),
            Self::Topic { name } => topic_collection_name(name),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum WikiSourceKind {
    Raw,
    SourceNote,
    Concept,
    Topic,
    Inbox,
}

impl WikiSourceKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Raw => "raw",
            Self::SourceNote => "source_note",
            Self::Concept => "concept",
            Self::Topic => "topic",
            Self::Inbox => "inbox",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WikiProvenance {
    pub source_path: String,
    pub captured_from: Option<String>,
    pub content_hash: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WikiDocumentRow {
    pub id: String,
    pub scope_kind: String,
    pub scope_id: String,
    pub project_id: Option<String>,
    pub topic_name: Option<String>,
    pub path: String,
    pub source_kind: WikiSourceKind,
    pub content_hash: String,
    pub frontmatter: Value,
    pub provenance: Value,
}

pub fn project_collection_name(project_id: &str) -> String {
    format!("gwiki:project:{project_id}")
}

pub fn topic_collection_name(topic_name: &str) -> String {
    format!("gwiki:topic:{topic_name}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn derived_storage_names_are_namespaced() {
        assert_eq!(WIKI_DOC_LABEL, "WikiDoc");
        assert_eq!(WIKI_SOURCE_LABEL, "WikiSource");
        assert_eq!(WIKI_TOPIC_LABEL, "WikiTopic");
        assert_eq!(
            project_collection_name("project-123"),
            "gwiki:project:project-123"
        );
        assert_eq!(topic_collection_name("rust"), "gwiki:topic:rust");

        let project_scope = WikiScope::Project {
            project_id: "project-123".to_string(),
        };
        assert_eq!(project_scope.kind(), "project");
        assert_eq!(project_scope.identity(), "project-123");
        assert_eq!(project_scope.project_id(), Some("project-123"));
        assert_eq!(
            project_scope.vector_collection_name(),
            "gwiki:project:project-123"
        );

        let topic_scope = WikiScope::Topic {
            name: "rust".to_string(),
        };
        assert_eq!(topic_scope.kind(), "topic");
        assert_eq!(topic_scope.identity(), "rust");
        assert_eq!(topic_scope.topic_name(), Some("rust"));
        assert_eq!(topic_scope.vector_collection_name(), "gwiki:topic:rust");
    }
}
