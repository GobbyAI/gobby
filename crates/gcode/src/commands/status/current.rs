use std::fmt::Write as _;
use std::path::Path;

use crate::config::Context;
use crate::db;
use crate::models::IndexedProject;
use crate::output::{self, Format};
use crate::utils::short_id;
use crate::visibility;

use super::shared::{format_coverage, format_timestamp, indexed_project_from_row};

pub fn run(ctx: &Context, format: Format) -> anyhow::Result<()> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;

    let stats: Option<IndexedProject> = conn
        .query_opt(
            "SELECT id,
                    root_path,
                    total_files::BIGINT AS total_files,
                    total_symbols::BIGINT AS total_symbols,
                    last_indexed_at::TEXT AS last_indexed_at,
                    COALESCE(index_duration_ms, 0)::BIGINT AS index_duration_ms,
                    NULL::BIGINT AS total_eligible_files
             FROM code_indexed_projects WHERE id = $1",
            &[&ctx.project_id],
        )
        .ok()
        .flatten()
        .and_then(|row| indexed_project_from_row(&row).ok());

    match stats {
        Some(s) => match format {
            Format::Json => {
                let mut value = serde_json::to_value(&s)?;
                if let Some(overlay) = overlay_status_json(ctx, &mut conn) {
                    value["overlay"] = overlay;
                }
                output::print_json(&value)
            }
            Format::Text => {
                let name = Path::new(&s.root_path)
                    .file_name()
                    .map(|n| n.to_string_lossy().to_string())
                    .unwrap_or_else(|| s.id.clone());
                let mut text = String::new();
                writeln!(text, "{} ({})", name, short_id(&s.id))?;
                writeln!(text, "  Root:     {}", s.root_path)?;
                writeln!(
                    text,
                    "  Files:    {}",
                    format_coverage(s.total_files, s.total_eligible_files)
                )?;
                writeln!(text, "  Symbols:  {}", s.total_symbols)?;
                writeln!(text, "  Indexed:  {}", format_timestamp(&s.last_indexed_at))?;
                write!(text, "  Duration: {}ms", s.index_duration_ms)?;
                if let crate::config::ProjectIndexScope::Overlay {
                    parent_project_id,
                    parent_root,
                    ..
                } = &ctx.index_scope
                {
                    writeln!(text)?;
                    write!(
                        text,
                        "  Overlay:  parent {} ({})",
                        parent_root.display(),
                        short_id(parent_project_id)
                    )?;
                    let tombstones = visibility::tombstone_count(&mut conn, ctx);
                    if tombstones > 0 {
                        writeln!(text)?;
                        write!(text, "  Deletes:  {tombstones}")?;
                    }
                }
                output::print_text(&text)
            }
        },
        None => {
            eprintln!(
                "No index found for project {}. Run `gcode index` first.",
                ctx.project_id
            );
            Ok(())
        }
    }
}

fn overlay_status_json(ctx: &Context, conn: &mut postgres::Client) -> Option<serde_json::Value> {
    let crate::config::ProjectIndexScope::Overlay {
        overlay_project_id,
        overlay_root,
        parent_project_id,
        parent_root,
    } = &ctx.index_scope
    else {
        return None;
    };

    let tombstones = visibility::tombstone_count(conn, ctx);
    let mut overlay = serde_json::json!({
        "overlay_project_id": overlay_project_id,
        "overlay_root": overlay_root,
        "parent_project_id": parent_project_id,
        "parent_root": parent_root,
    });
    if tombstones > 0 {
        overlay["tombstones"] = serde_json::json!(tombstones);
    }
    Some(overlay)
}
