use std::path::{Component, Path, PathBuf};

use crate::WikiError;
use crate::sources::SourceRecord;

pub(crate) fn validate_source_id(id: &str) -> Result<&str, WikiError> {
    let id = id.trim();
    if id.is_empty()
        || id.contains("..")
        || id.contains('/')
        || id.contains('\\')
        || Path::new(id)
            .components()
            .any(|component| !matches!(component, Component::Normal(_)))
    {
        return Err(WikiError::InvalidInput {
            field: "source_id",
            message: format!("unsafe source id `{id}`"),
        });
    }
    Ok(id)
}

pub(crate) fn raw_source_path(id: &str) -> Result<PathBuf, WikiError> {
    let id = validate_source_id(id)?;
    Ok(Path::new("raw").join(format!("{id}.md")))
}

pub(crate) fn derived_markdown_path(record: &SourceRecord) -> Result<PathBuf, WikiError> {
    let id = validate_source_id(&record.id)?;
    Ok(PathBuf::from("knowledge")
        .join("sources")
        .join(format!("{id}.md")))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sources::{CompileStatus, IngestionMethod, SourceKind};

    #[test]
    fn source_paths_trim_safe_ids() {
        assert_eq!(
            raw_source_path("  src-abc  ").expect("raw path"),
            PathBuf::from("raw/src-abc.md")
        );
    }

    #[test]
    fn source_paths_reject_unsafe_ids() {
        for id in ["", ".", "..", "src/abc", r"src\abc", "src..abc"] {
            let error = raw_source_path(id).expect_err("unsafe source id rejected");
            assert_eq!(error.code(), "invalid_input");
        }
    }

    #[test]
    fn derived_markdown_path_rejects_unsafe_source_ids() {
        let mut record = source_record("src/abc");

        let error = derived_markdown_path(&record).expect_err("unsafe derived path rejected");
        assert_eq!(error.code(), "invalid_input");

        record.id = "src-abc".to_string();
        assert_eq!(
            derived_markdown_path(&record).expect("derived path"),
            PathBuf::from("knowledge/sources/src-abc.md")
        );
    }

    fn source_record(id: &str) -> SourceRecord {
        SourceRecord {
            id: id.to_string(),
            location: "https://example.test/source".to_string(),
            canonical_location: "https://example.test/source".to_string(),
            kind: SourceKind::Url,
            fetched_at: "2026-01-01T00:00:00Z".to_string(),
            content_hash: "hash".to_string(),
            title: None,
            citation: None,
            license: None,
            ingestion_method: IngestionMethod::Manual,
            compile_status: CompileStatus::Pending,
            replay: None,
        }
    }
}
