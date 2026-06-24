use std::collections::BTreeMap;
use std::path::Path;

use crate::config::Context;
use crate::db;
use crate::output::{self, Format};
use crate::visibility;

pub fn repo_outline(ctx: &Context, format: Format) -> anyhow::Result<()> {
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
            .map(|p| p.to_string_lossy().to_string())
            .unwrap_or_else(|| ".".to_string());
        dirs.entry(dir).or_default().push(f);
    }

    match format {
        Format::Json => output::print_json(&dirs),
        Format::Text => {
            for (dir, dir_files) in &dirs {
                let total_syms: i64 = dir_files
                    .iter()
                    .map(|f| f["symbol_count"].as_i64().unwrap_or(0))
                    .sum();
                println!("{dir}/ ({} files, {total_syms} symbols)", dir_files.len());
            }
            Ok(())
        }
    }
}
