//! `gwiki pages` — lightweight DB-backed listing of indexed wiki pages plus a
//! filesystem walk of the vault's unindexed `outputs/` markdown reports, so
//! UI file trees never need the multi-MB graph payload.

use std::path::Path;

use chrono::{DateTime, Utc};
use serde::Serialize;
use serde_json::json;

use crate::search::SearchScope;
use crate::support::env::database_url_for;
use crate::support::scope::resolve_selection_context;
use crate::{CommandOutcome, ScopeIdentity, ScopeSelection, WikiError};

/// One indexed wiki page in the listing payload.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub(crate) struct PageEntry {
    path: String,
    title: String,
    tags: Vec<String>,
    content_hash: String,
    updated_at: String,
}

/// One unindexed markdown report under `outputs/`.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub(crate) struct OutputEntry {
    path: String,
    size: u64,
    modified: String,
}

pub(crate) fn execute(
    selection: ScopeSelection,
    prefix: Option<String>,
) -> Result<CommandOutcome, WikiError> {
    let resolved = resolve_selection_context(&selection)?;
    let database_url = database_url_for("gwiki pages")?.ok_or_else(|| WikiError::Config {
        detail: "gwiki pages requires PostgreSQL index configuration".to_string(),
    })?;
    let mut conn = gobby_core::postgres::connect_readonly(&database_url).map_err(|error| {
        WikiError::Config {
            detail: format!("failed to connect to PostgreSQL for gwiki pages: {error}"),
        }
    })?;

    let pages = load_page_entries(&mut conn, &resolved.search_scope)?;
    let pages = filter_by_prefix(pages, prefix.as_deref());
    let outputs = collect_output_entries(resolved.scope.root())?;
    Ok(pages_outcome(&resolved.output_scope, pages, outputs))
}

fn load_page_entries(
    conn: &mut postgres::Client,
    scope: &SearchScope,
) -> Result<Vec<PageEntry>, WikiError> {
    let scope_kind = scope.scope_kind().to_string();
    let scope_id = scope.scope_value().to_string();
    let rows = conn
        .query(
            "SELECT path, title, frontmatter, content_hash, updated_at
             FROM gwiki_documents
             WHERE scope_kind = $1 AND scope_id = $2
             ORDER BY path",
            &[&scope_kind, &scope_id],
        )
        .map_err(|error| WikiError::Config {
            detail: format!("failed to load the gwiki pages listing: {error}"),
        })?;
    Ok(rows
        .into_iter()
        .map(|row| {
            let path: String = row.get("path");
            let title = row
                .get::<_, Option<String>>("title")
                .unwrap_or_else(|| default_title(&path));
            let updated_at: std::time::SystemTime = row.get("updated_at");
            PageEntry {
                title,
                tags: frontmatter_tags(&row.get::<_, serde_json::Value>("frontmatter")),
                content_hash: row.get("content_hash"),
                updated_at: DateTime::<Utc>::from(updated_at).to_rfc3339(),
                path,
            }
        })
        .collect())
}

fn default_title(path: &str) -> String {
    path.rsplit('/')
        .next()
        .unwrap_or(path)
        .trim_end_matches(".md")
        .to_string()
}

fn frontmatter_tags(frontmatter: &serde_json::Value) -> Vec<String> {
    match frontmatter.get("tags") {
        Some(serde_json::Value::Array(values)) => values
            .iter()
            .filter_map(|value| value.as_str().map(str::to_string))
            .collect(),
        Some(serde_json::Value::String(value)) => vec![value.clone()],
        _ => Vec::new(),
    }
}

fn filter_by_prefix(pages: Vec<PageEntry>, prefix: Option<&str>) -> Vec<PageEntry> {
    match prefix {
        Some(prefix) => pages
            .into_iter()
            .filter(|page| page.path.starts_with(prefix))
            .collect(),
        None => pages,
    }
}

/// Walk `<root>/outputs` for markdown reports. Outputs stay unindexed by
/// design, so this is the only listing surface they appear on.
fn collect_output_entries(root: &Path) -> Result<Vec<OutputEntry>, WikiError> {
    let outputs_root = root.join("outputs");
    if !outputs_root.is_dir() {
        return Ok(Vec::new());
    }

    let mut entries = Vec::new();
    let mut pending = vec![outputs_root];
    while let Some(dir) = pending.pop() {
        let reader = std::fs::read_dir(&dir).map_err(|error| WikiError::Io {
            action: "walk wiki outputs directory",
            path: Some(dir.clone()),
            source: error,
        })?;
        for dir_entry in reader {
            let dir_entry = dir_entry.map_err(|error| WikiError::Io {
                action: "walk wiki outputs directory",
                path: Some(dir.clone()),
                source: error,
            })?;
            let path = dir_entry.path();
            if path.is_dir() {
                pending.push(path);
                continue;
            }
            if path.extension().and_then(|extension| extension.to_str()) != Some("md") {
                continue;
            }
            let metadata = std::fs::metadata(&path).map_err(|error| WikiError::Io {
                action: "stat wiki outputs report",
                path: Some(path.clone()),
                source: error,
            })?;
            let modified = metadata.modified().map_err(|error| WikiError::Io {
                action: "stat wiki outputs report",
                path: Some(path.clone()),
                source: error,
            })?;
            let relative = path.strip_prefix(root).unwrap_or(&path);
            entries.push(OutputEntry {
                path: relative.display().to_string(),
                size: metadata.len(),
                modified: DateTime::<Utc>::from(modified).to_rfc3339(),
            });
        }
    }
    entries.sort_by(|left, right| left.path.cmp(&right.path));
    Ok(entries)
}

fn pages_outcome(
    scope: &ScopeIdentity,
    pages: Vec<PageEntry>,
    outputs: Vec<OutputEntry>,
) -> CommandOutcome {
    let text = format!(
        "Listed {} wiki pages and {} outputs reports\nScope: {scope}",
        pages.len(),
        outputs.len()
    );
    let payload = json!({
        "command": "pages",
        "scope": scope,
        "pages": pages,
        "outputs": outputs,
    });
    super::scoped_outcome("pages", scope, payload, text)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ScopeIdentity;
    use serde_json::json;

    fn entry(path: &str, tags: &[&str]) -> PageEntry {
        PageEntry {
            path: path.to_string(),
            title: path
                .rsplit('/')
                .next()
                .unwrap_or(path)
                .trim_end_matches(".md")
                .to_string(),
            tags: tags.iter().map(|tag| tag.to_string()).collect(),
            content_hash: format!("hash-{path}"),
            updated_at: "2026-07-10T00:00:00+00:00".to_string(),
        }
    }

    #[test]
    fn prefix_filters_listing() {
        let pages = vec![
            entry("code/gobby/src/runner.md", &[]),
            entry("knowledge/concepts/gobby.md", &["rust"]),
            entry("recaps/2026-07-09.md", &[]),
        ];

        let filtered = filter_by_prefix(pages.clone(), Some("code/"));
        assert_eq!(filtered.len(), 1);
        assert_eq!(filtered[0].path, "code/gobby/src/runner.md");

        assert_eq!(filter_by_prefix(pages.clone(), None).len(), 3);
        assert!(filter_by_prefix(pages, Some("outputs/")).is_empty());
    }

    #[test]
    fn frontmatter_tags_accepts_arrays_and_strings() {
        assert_eq!(
            frontmatter_tags(&json!({"tags": ["rust", "wiki"]})),
            vec!["rust".to_string(), "wiki".to_string()]
        );
        assert_eq!(
            frontmatter_tags(&json!({"tags": "rust"})),
            vec!["rust".to_string()]
        );
        assert!(frontmatter_tags(&json!({})).is_empty());
        assert!(frontmatter_tags(&json!({"tags": [1, true]})).is_empty());
    }

    #[test]
    fn collect_output_entries_lists_markdown_reports() {
        let temp = tempfile::tempdir().expect("tempdir");
        let outputs = temp.path().join("outputs");
        std::fs::create_dir_all(outputs.join("reports")).expect("outputs dirs");
        std::fs::write(outputs.join("GRAPH_REPORT.md"), "# Graph").expect("report");
        std::fs::write(outputs.join("reports").join("health.md"), "# Health").expect("health");
        std::fs::write(outputs.join("graph.json"), "{}").expect("json artifact");

        let entries = collect_output_entries(temp.path()).expect("collect outputs");
        let paths = entries
            .iter()
            .map(|entry| entry.path.as_str())
            .collect::<Vec<_>>();
        assert_eq!(
            paths,
            vec!["outputs/GRAPH_REPORT.md", "outputs/reports/health.md"]
        );
        assert_eq!(entries[0].size, "# Graph".len() as u64);
        assert!(!entries[0].modified.is_empty());
    }

    #[test]
    fn collect_output_entries_handles_missing_directory() {
        let temp = tempfile::tempdir().expect("tempdir");
        let entries = collect_output_entries(temp.path()).expect("collect outputs");
        assert!(entries.is_empty());
    }

    #[test]
    fn pages_outcome_emits_envelope() {
        let scope = ScopeIdentity::project("proj-1");
        let outcome = pages_outcome(
            &scope,
            vec![entry("knowledge/concepts/gobby.md", &["rust"])],
            Vec::new(),
        );

        let payload = outcome.result.payload;
        assert_eq!(payload["command"], "pages");
        assert_eq!(payload["scope"]["id"], "proj-1");
        assert_eq!(payload["pages"][0]["path"], "knowledge/concepts/gobby.md");
        assert_eq!(payload["pages"][0]["tags"][0], "rust");
        assert!(payload["pages"][0]["content_hash"].is_string());
        assert!(payload["pages"][0]["updated_at"].is_string());
        assert!(
            payload["outputs"]
                .as_array()
                .expect("outputs array")
                .is_empty()
        );
    }
}
