use std::path::Path;

use crate::bm25_health;
use crate::config::{Context, ProjectIndexScope, parent_code_index_exists};
use crate::db;
use crate::models::IndexedProject;
use crate::node::{OverlayStatus, StatusReport};
use crate::output::{self, Format};
use crate::utils::short_id;
use crate::visibility;

use super::shared::{format_coverage, format_timestamp, indexed_project_from_row};

/// Collects the status report from the database. Reads no file on disk.
pub fn collect(conn: &mut postgres::Client, ctx: &Context) -> anyhow::Result<StatusReport> {
    let code_index = bm25_health::verify(conn);
    // A missing parent index reads as unindexed, the same report an empty overlay gets.
    let parent_indexed = match &ctx.index_scope {
        ProjectIndexScope::Overlay {
            parent_project_id, ..
        } => parent_code_index_exists(conn, parent_project_id, &ctx.machine_id)?,
        ProjectIndexScope::Single => true,
    };

    let stats: Option<IndexedProject> = db::id_param(&ctx.project_id)
        .ok()
        .filter(|_| parent_indexed)
        .and_then(|project_id| {
            let machine_id = db::id_param(&ctx.machine_id).ok()?;
            conn.query_opt(
                "SELECT project_id AS id,
                    root_path,
                    total_files::BIGINT AS total_files,
                    total_symbols::BIGINT AS total_symbols,
                    last_indexed_at::TEXT AS last_indexed_at,
                    COALESCE(index_duration_ms, 0)::BIGINT AS index_duration_ms,
                    NULL::BIGINT AS total_eligible_files,
                    indexer_version
             FROM code_indexed_project_states
             WHERE machine_id = $1 AND project_id = $2",
                &[&machine_id, &project_id],
            )
            .ok()
            .flatten()
        })
        .and_then(|row| indexed_project_from_row(&row).ok());

    Ok(match stats {
        Some(project) => StatusReport::Indexed {
            overlay: overlay_status(ctx, conn),
            project: Box::new(project),
            code_index,
        },
        None => StatusReport::Unindexed {
            project_id: ctx.project_id.clone(),
            indexed: false,
            code_index,
        },
    })
}

/// Renders the JSON status object, without a trailing newline.
pub fn render_json(report: &StatusReport) -> anyhow::Result<String> {
    // Going through a Value sorts every key, which is the order the CLI has always printed.
    Ok(serde_json::to_string(&serde_json::to_value(report)?)?)
}

/// Renders the text status, without a trailing newline.
pub fn render_text(report: &StatusReport) -> String {
    match report {
        StatusReport::Indexed {
            project,
            overlay,
            code_index,
        } => {
            let name = Path::new(&project.root_path)
                .file_name()
                .map(|n| n.to_string_lossy().to_string())
                .unwrap_or_else(|| project.id.clone());
            let mut lines = vec![
                format!("{} ({})", name, short_id(&project.id)),
                format!("  Root:     {}", project.root_path),
                format!(
                    "  Files:    {}",
                    format_coverage(project.total_files, project.total_eligible_files)
                ),
                format!("  Symbols:  {}", project.total_symbols),
                format!("  Indexed:  {}", format_timestamp(&project.last_indexed_at)),
                format!("  Duration: {}ms", project.index_duration_ms),
            ];
            if let Some(overlay) = overlay {
                lines.push(format!(
                    "  Overlay:  parent {} ({})",
                    overlay.parent_root.display(),
                    short_id(&overlay.parent_project_id)
                ));
                if overlay.tombstones > 0 {
                    lines.push(format!("  Deletes:  {}", overlay.tombstones));
                }
            }
            format!("{}\n{}", lines.join("\n"), code_index.render_text())
        }
        StatusReport::Unindexed { code_index, .. } => code_index.render_text(),
    }
}

pub fn run(ctx: &Context, format: Format) -> anyhow::Result<()> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let report = collect(&mut conn, ctx)?;
    match format {
        Format::Json => output::print_text(&render_json(&report)?),
        Format::Text => {
            if let StatusReport::Unindexed { project_id, .. } = &report {
                eprintln!("No index found for project {project_id}. Run `gcode index` first.");
            }
            output::print_text(&render_text(&report))
        }
    }
}

fn overlay_status(ctx: &Context, conn: &mut postgres::Client) -> Option<OverlayStatus> {
    let ProjectIndexScope::Overlay {
        overlay_project_id,
        overlay_root,
        parent_project_id,
        parent_root,
    } = &ctx.index_scope
    else {
        return None;
    };

    Some(OverlayStatus {
        overlay_project_id: overlay_project_id.clone(),
        overlay_root: overlay_root.clone(),
        parent_project_id: parent_project_id.clone(),
        parent_root: parent_root.clone(),
        tombstones: visibility::tombstone_count(conn, ctx),
    })
}
