use std::path::Path;

use crate::WikiError;
use crate::ingest::{
    IngestResult, markdown_title, single_line, text_from_utf8_lossy, write_raw_then_index,
};
use crate::sources::{CompileStatus, IngestionMethod, SourceDraft, SourceKind, SourceManifest};
use crate::store::WikiIndexStore;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GitFileSnapshot {
    pub path: String,
    pub bytes: Vec<u8>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GitRepositorySnapshot {
    pub remote_url: String,
    pub commit_sha: String,
    pub fetched_at: String,
    pub files: Vec<GitFileSnapshot>,
}

pub fn ingest_repository(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    snapshot: GitRepositorySnapshot,
) -> Result<IngestResult, WikiError> {
    if snapshot.files.is_empty() {
        return Err(WikiError::InvalidInput {
            field: "files",
            message: "git repository ingest requires at least one selected file".to_string(),
        });
    }

    let location = format!("git+{}@{}", snapshot.remote_url, snapshot.commit_sha);
    let title = markdown_title(&snapshot.remote_url);
    let draft = SourceDraft {
        location,
        kind: SourceKind::GitRepository,
        fetched_at: snapshot.fetched_at.clone(),
        content: snapshot_content_bytes(&snapshot),
        title: Some(title.clone()),
        citation: Some(format!("{} @ {}", snapshot.remote_url, snapshot.commit_sha)),
        license: None,
        ingestion_method: IngestionMethod::Manual,
        compile_status: CompileStatus::Pending,
    };
    let record = SourceManifest::register(vault_root, draft)?;
    let markdown = render_git_markdown(&snapshot, &title, &record.content_hash);
    write_raw_then_index(vault_root, store, record, &markdown, None)
}

fn snapshot_content_bytes(snapshot: &GitRepositorySnapshot) -> Vec<u8> {
    let mut content = Vec::new();
    content.extend_from_slice(snapshot.remote_url.as_bytes());
    content.push(b'\n');
    content.extend_from_slice(snapshot.commit_sha.as_bytes());
    content.push(b'\n');
    for file in &snapshot.files {
        content.extend_from_slice(b"\n-- ");
        content.extend_from_slice(file.path.as_bytes());
        content.extend_from_slice(b" --\n");
        content.extend_from_slice(&file.bytes);
        if !file.bytes.ends_with(b"\n") {
            content.push(b'\n');
        }
    }
    content
}

fn render_git_markdown(snapshot: &GitRepositorySnapshot, title: &str, source_hash: &str) -> String {
    let mut markdown = git_markdown_metadata(&[
        ("source_kind", "git_repository".to_string()),
        ("git_remote", snapshot.remote_url.clone()),
        ("git_commit", snapshot.commit_sha.clone()),
        ("fetched_at", snapshot.fetched_at.clone()),
        ("source_hash", source_hash.to_string()),
    ]);
    markdown.push_str("# ");
    markdown.push_str(title);
    markdown.push_str("\n\n");

    for file in &snapshot.files {
        markdown.push_str("## ");
        markdown.push_str(&markdown_title(&file.path));
        markdown.push_str("\n\n");
        markdown.push_str("file_path: ");
        markdown.push_str(&single_line(&file.path));
        let file_text = text_from_utf8_lossy(&file.bytes);
        let fence = markdown_code_fence(&file_text);
        markdown.push_str("\n\n");
        markdown.push_str(&fence);
        markdown.push_str(&code_fence_info(&file.path));
        markdown.push('\n');
        markdown.push_str(&file_text);
        if !markdown.ends_with('\n') {
            markdown.push('\n');
        }
        markdown.push_str(&fence);
        markdown.push_str("\n\n");
    }
    markdown
}

fn git_markdown_metadata(fields: &[(&str, String)]) -> String {
    let mut metadata = String::from("---\n");
    for (key, value) in fields {
        metadata.push_str(key);
        metadata.push_str(": ");
        metadata.push_str(
            &serde_json::to_string(&single_line(value)).expect("string scalar serializes"),
        );
        metadata.push('\n');
    }
    metadata.push_str("---\n\n");
    metadata
}

fn code_fence_info(path: &str) -> String {
    Path::new(path)
        .extension()
        .and_then(|extension| extension.to_str())
        .map(|extension| {
            extension
                .chars()
                .filter(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '_' | '-'))
                .collect::<String>()
        })
        .filter(|extension| !extension.is_empty())
        .unwrap_or_else(|| "text".to_string())
}

fn markdown_code_fence(text: &str) -> String {
    let mut max_run = 0usize;
    let mut current_run = 0usize;
    for ch in text.chars() {
        if ch == '`' {
            current_run += 1;
            max_run = max_run.max(current_run);
        } else {
            current_run = 0;
        }
    }
    "`".repeat(max_run.saturating_add(1).max(3))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sources::{SourceKind, SourceManifest};
    use crate::store::MemoryWikiStore;

    #[test]
    fn git_ingest_records_commit_provenance() {
        let temp = tempfile::tempdir().expect("tempdir");
        let snapshot = GitRepositorySnapshot {
            remote_url: "https://github.com/GobbyAI/example.git".to_string(),
            commit_sha: "7f83b1657ff1fc53b92dc18148a1d65dfa135adb".to_string(),
            fetched_at: "2026-05-29T18:20:00Z".to_string(),
            files: vec![
                GitFileSnapshot {
                    path: "README.md".to_string(),
                    bytes: b"# Example\n\nRepository notes.\n".to_vec(),
                },
                GitFileSnapshot {
                    path: "src/lib.rs".to_string(),
                    bytes: b"pub fn answer() -> u8 { 42 }\n".to_vec(),
                },
            ],
        };
        let mut store = MemoryWikiStore::default();

        let result =
            ingest_repository(temp.path(), &mut store, snapshot).expect("ingest git repository");

        let raw = std::fs::read_to_string(temp.path().join(&result.raw_path))
            .expect("raw markdown written");
        assert!(raw.contains("# https://github.com/GobbyAI/example.git"));
        assert!(raw.contains("source_kind: \"git_repository\""));
        assert!(raw.contains("git_remote: \"https://github.com/GobbyAI/example.git\""));
        assert!(raw.contains("git_commit: \"7f83b1657ff1fc53b92dc18148a1d65dfa135adb\""));
        assert!(raw.contains("file_path: README.md"));
        assert!(raw.contains("file_path: src/lib.rs"));
        assert!(raw.contains("Repository notes."));
        assert!(raw.contains("pub fn answer() -> u8 { 42 }"));

        let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
        assert_eq!(manifest.entries.len(), 1);
        let entry = &manifest.entries[0];
        assert_eq!(entry.kind, SourceKind::GitRepository);
        assert_eq!(
            entry.canonical_location,
            "git+https://github.com/GobbyAI/example.git@7f83b1657ff1fc53b92dc18148a1d65dfa135adb"
        );
        assert_eq!(entry.fetched_at, "2026-05-29T18:20:00Z");
    }
}
