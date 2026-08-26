use std::collections::BTreeMap;
use std::path::Path;

use serde::Serialize;

use crate::commands::token_budget;
use crate::config::Context;
use crate::db;
use crate::output::Format;
use crate::visibility;

#[derive(Serialize)]
struct DirectorySummary {
    directory: String,
    file_count: usize,
    symbol_count: i64,
    files: Vec<serde_json::Value>,
}

pub fn repo_outline(
    ctx: &Context,
    limit: Option<usize>,
    offset: usize,
    token_budget: Option<usize>,
    format: Format,
) -> anyhow::Result<()> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;

    let files: Vec<serde_json::Value> = visibility::visible_tree(&mut conn, ctx)?
        .into_iter()
        .map(|file| {
            serde_json::json!({
                "file_path": file.file_path,
                "language": file.language,
                "symbol_count": file.symbol_count,
            })
        })
        .collect();

    let mut dirs: BTreeMap<String, Vec<&serde_json::Value>> = BTreeMap::new();
    for f in &files {
        let fp = f["file_path"].as_str().unwrap_or("");
        let dir = Path::new(fp)
            .parent()
            .and_then(|p| {
                let path = p.to_string_lossy();
                if path.is_empty() {
                    None
                } else {
                    Some(path.to_string())
                }
            })
            .unwrap_or_else(|| ".".to_string());
        dirs.entry(dir).or_default().push(f);
    }

    let summaries = dirs
        .into_iter()
        .map(|(directory, files)| DirectorySummary {
            directory,
            file_count: files.len(),
            symbol_count: files
                .iter()
                .map(|file| file["symbol_count"].as_i64().unwrap_or(0))
                .sum(),
            files: files.into_iter().cloned().collect(),
        })
        .collect();
    let (total, limit, summaries) = token_budget::window(summaries, offset, limit);
    let meta = token_budget::CollectionPageMeta {
        project_id: &ctx.project_id,
        total,
        offset,
        limit,
        hint: None,
    };
    let page = token_budget::paginate(summaries, meta, token_budget, format, render_text);
    token_budget::print_page(&page, meta, format, render_text)
}

fn render_text(summaries: &[DirectorySummary]) -> String {
    summaries
        .iter()
        .map(|summary| {
            format!(
                "{}/ ({} files, {} symbols)",
                summary.directory, summary.file_count, summary.symbol_count
            )
        })
        .collect::<Vec<_>>()
        .join("\n")
}
