use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use serde::Serialize;

use crate::WikiError;
use crate::provenance::ProvenanceGraph;

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CitationDriftIssue {
    pub page_path: PathBuf,
    pub section_id: String,
    pub expected_content_hash: String,
    pub current_content_hash: Option<String>,
    pub source_ids: Vec<String>,
}

pub(super) fn inspect(
    vault_root: &Path,
    provenance: &ProvenanceGraph,
) -> Result<Vec<CitationDriftIssue>, WikiError> {
    let mut grouped: BTreeMap<(PathBuf, String, String), BTreeSet<String>> = BTreeMap::new();
    for link in provenance.links() {
        grouped
            .entry((
                link.section.page_path.clone(),
                link.section.section_id.clone(),
                link.section.content_hash.clone(),
            ))
            .or_default()
            .insert(link.source.source_id.clone());
    }

    let mut current_hashes: BTreeMap<PathBuf, Option<String>> = BTreeMap::new();
    let mut issues = Vec::new();
    for ((page_path, section_id, expected_content_hash), source_ids) in grouped {
        let current_content_hash = match current_hashes.get(&page_path) {
            Some(hash) => hash.clone(),
            None => {
                let hash = current_content_hash(vault_root, &page_path)?;
                current_hashes.insert(page_path.clone(), hash.clone());
                hash
            }
        };
        if current_content_hash.as_deref() == Some(expected_content_hash.as_str()) {
            continue;
        }
        issues.push(CitationDriftIssue {
            page_path,
            section_id,
            expected_content_hash,
            current_content_hash,
            source_ids: source_ids.into_iter().collect(),
        });
    }
    Ok(issues)
}

pub(super) fn render_text(text: &mut String, issues: &[CitationDriftIssue]) {
    text.push_str("\n## Citation drift\n\n");
    if issues.is_empty() {
        text.push_str("- None\n");
        return;
    }
    for issue in issues {
        text.push_str("- `");
        text.push_str(&issue.page_path.display().to_string());
        text.push_str("` #");
        text.push_str(&issue.section_id);
        text.push_str(": expected `");
        text.push_str(&issue.expected_content_hash);
        match &issue.current_content_hash {
            Some(current) => {
                text.push_str("`, current `");
                text.push_str(current);
                text.push('`');
            }
            None => text.push_str("`, page missing"),
        }
        text.push_str(" (sources: ");
        text.push_str(&issue.source_ids.join(", "));
        text.push_str(")\n");
    }
}

fn current_content_hash(vault_root: &Path, page_path: &Path) -> Result<Option<String>, WikiError> {
    match crate::page_version::content_hash_from_path(&vault_root.join(page_path)) {
        Ok(hash) => Ok(Some(hash)),
        Err(WikiError::Io { source, .. }) if source.kind() == std::io::ErrorKind::NotFound => {
            Ok(None)
        }
        Err(error) => Err(error),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::provenance::{ProvenanceLink, SourceChunkRef, WikiSectionRef};

    fn link(page_hash: &str, source_id: &str) -> ProvenanceLink {
        ProvenanceLink {
            source: SourceChunkRef {
                source_id: source_id.to_string(),
                chunk_id: format!("{source_id}#chunk-0"),
                path: PathBuf::from(format!("raw/{source_id}.md")),
                source_hash: format!("{source_id}-hash"),
                byte_start: 0,
                byte_end: 4,
            },
            section: WikiSectionRef {
                page_path: PathBuf::from("knowledge/topics/example.md"),
                heading: "Overview".to_string(),
                section_id: "overview".to_string(),
                content_hash: page_hash.to_string(),
            },
            claim: Some("fact".to_string()),
        }
    }

    #[test]
    fn matching_page_has_no_citation_drift() {
        let temp = tempfile::tempdir().expect("tempdir");
        let page_path = PathBuf::from("knowledge/topics/example.md");
        let markdown = "---\nlifecycle: draft\n---\n# Example\n\nBody.\n";
        std::fs::create_dir_all(temp.path().join(&page_path).parent().expect("page parent"))
            .expect("create page parent");
        std::fs::write(temp.path().join(&page_path), markdown).expect("write page");
        let mut provenance = ProvenanceGraph::default();
        provenance.replace_page_links(
            &page_path,
            [link(
                &crate::page_version::content_hash(markdown),
                "source-b",
            )],
        );

        assert!(
            inspect(temp.path(), &provenance)
                .expect("inspect")
                .is_empty()
        );
    }

    #[test]
    fn mutated_and_missing_pages_report_deterministic_drift() {
        let temp = tempfile::tempdir().expect("tempdir");
        let page_path = PathBuf::from("knowledge/topics/example.md");
        let markdown = "# Example\n\nChanged.\n";
        std::fs::create_dir_all(temp.path().join(&page_path).parent().expect("page parent"))
            .expect("create page parent");
        std::fs::write(temp.path().join(&page_path), markdown).expect("write page");
        let mut provenance = ProvenanceGraph::default();
        provenance.replace_page_links(
            &page_path,
            [link("expected", "source-b"), link("expected", "source-a")],
        );

        let drift = inspect(temp.path(), &provenance).expect("inspect mutation");
        assert_eq!(drift.len(), 1);
        assert_eq!(drift[0].expected_content_hash, "expected");
        assert_eq!(
            drift[0].current_content_hash,
            Some(crate::page_version::content_hash(markdown))
        );
        assert_eq!(drift[0].source_ids, ["source-a", "source-b"]);

        std::fs::remove_file(temp.path().join(page_path)).expect("remove page");
        let missing = inspect(temp.path(), &provenance).expect("inspect missing");
        assert_eq!(missing.len(), 1);
        assert_eq!(missing[0].current_content_hash, None);
    }
}
