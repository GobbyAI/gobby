use std::path::Path;

use crate::WikiError;
use crate::ingest::{
    IngestResult, markdown_metadata, markdown_title, single_line, write_raw_then_index,
};
use crate::sources::{CompileStatus, IngestionMethod, SourceDraft, SourceKind, SourceManifest};
use crate::store::WikiIndexStore;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MediaWikiPageSnapshot {
    pub title: String,
    pub source_url: String,
    pub revision_id: String,
    pub revision_timestamp: Option<String>,
    pub fetched_at: String,
    pub wikitext: String,
    pub category: Option<String>,
}

pub fn ingest_page(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    snapshot: MediaWikiPageSnapshot,
) -> Result<IngestResult, WikiError> {
    let title = markdown_title(&snapshot.title);
    let source_url = single_line(&snapshot.source_url);
    let draft = SourceDraft {
        location: source_url.clone(),
        kind: SourceKind::MediaWiki,
        fetched_at: snapshot.fetched_at.clone(),
        content: snapshot.wikitext.as_bytes().to_vec(),
        title: Some(title.clone()),
        citation: Some(source_url),
        license: None,
        ingestion_method: IngestionMethod::Manual,
        compile_status: CompileStatus::Pending,
    };
    let record = SourceManifest::register(vault_root, draft)?;
    let markdown = render_mediawiki_markdown(&snapshot, &title, &record.content_hash);
    write_raw_then_index(vault_root, store, record, &markdown, None)
}

fn render_mediawiki_markdown(
    snapshot: &MediaWikiPageSnapshot,
    title: &str,
    source_hash: &str,
) -> String {
    let mut fields = vec![
        ("source_kind", "mediawiki".to_string()),
        ("source_url", snapshot.source_url.clone()),
        ("page_title", snapshot.title.clone()),
        ("revision_id", snapshot.revision_id.clone()),
        ("fetched_at", snapshot.fetched_at.clone()),
        ("source_hash", source_hash.to_string()),
    ];
    if let Some(revision_timestamp) = &snapshot.revision_timestamp {
        fields.push(("revision_timestamp", revision_timestamp.clone()));
    }
    if let Some(category) = &snapshot.category {
        fields.push(("category", category.clone()));
    }

    let fields = fields
        .into_iter()
        .map(|(key, value)| (key, single_line(&value)))
        .collect::<Vec<_>>();
    let mut markdown = markdown_metadata(&fields);
    markdown.push_str("# ");
    markdown.push_str(title);
    markdown.push_str("\n\n");
    markdown.push_str(&snapshot.wikitext);
    if !markdown.ends_with('\n') {
        markdown.push('\n');
    }
    markdown
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sources::{SourceKind, SourceManifest};
    use crate::store::MemoryWikiStore;

    #[test]
    fn mediawiki_records_revision_metadata() {
        let temp = tempfile::tempdir().expect("tempdir");
        let snapshot = MediaWikiPageSnapshot {
            title: "Gobby\nEditor".to_string(),
            source_url: "https://wiki.example.test/wiki/Gobby\nEditor".to_string(),
            revision_id: "123456".to_string(),
            revision_timestamp: Some("2026-05-29T12:00:00Z".to_string()),
            fetched_at: "2026-05-29T18:00:00Z".to_string(),
            wikitext: "'''Gobby''' is a collaborative editor.".to_string(),
            category: Some("Software".to_string()),
        };
        let mut store = MemoryWikiStore::default();

        let result = ingest_page(temp.path(), &mut store, snapshot).expect("ingest mediawiki");

        let raw = std::fs::read_to_string(temp.path().join(&result.raw_path))
            .expect("raw markdown written");
        assert!(raw.contains("# Gobby Editor"));
        assert!(raw.contains("source_kind: mediawiki"));
        assert!(raw.contains("source_url: \"https://wiki.example.test/wiki/Gobby Editor\""));
        assert!(!raw.contains("source_url: \"https://wiki.example.test/wiki/Gobby\nEditor\""));
        assert!(raw.contains("revision_id: \"123456\""));
        assert!(raw.contains("revision_timestamp: \"2026-05-29T12:00:00Z\""));
        assert!(raw.contains("category: Software"));
        assert!(!raw.contains("\n\nCategory: Software\n\n"));
        assert!(raw.contains("'''Gobby''' is a collaborative editor."));

        let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
        assert_eq!(manifest.entries.len(), 1);
        let entry = &manifest.entries[0];
        assert_eq!(entry.kind, SourceKind::MediaWiki);
        assert_eq!(entry.title.as_deref(), Some("Gobby Editor"));
        assert_eq!(
            entry.citation.as_deref(),
            Some("https://wiki.example.test/wiki/Gobby Editor")
        );
        assert_eq!(entry.fetched_at, "2026-05-29T18:00:00Z");
    }
}
