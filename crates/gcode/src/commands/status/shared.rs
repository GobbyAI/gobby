use std::path::Path;

use crate::db;
use crate::models::IndexedProject;
use crate::utils::short_id;

/// Format a `last_indexed_at` value for display.
/// Handles both epoch seconds ("1774970556") and ISO 8601 ("2026-03-29T18:52:25.750230+00:00").
pub(super) fn format_timestamp(raw: &str) -> String {
    if raw.is_empty() {
        return "never".to_string();
    }

    // Try epoch seconds first (all digits)
    if let Ok(epoch) = raw.parse::<i64>() {
        let secs = epoch % 60;
        let mins = (epoch / 60) % 60;
        let hours = (epoch / 3600) % 24;
        let days = epoch / 86400;

        // Simple date calculation from days since epoch
        let (year, month, day) = days_to_ymd(days);
        return format!("{year:04}-{month:02}-{day:02} {hours:02}:{mins:02}:{secs:02} UTC");
    }

    // Try ISO 8601. Extract the date/time portion before any fractional seconds or timezone.
    if raw.len() >= 19 && raw.as_bytes().get(4) == Some(&b'-') {
        let base = &raw[..19]; // "2026-03-29T18:52:25"
        return base.replace('T', " ");
    }

    raw.to_string()
}

fn days_to_ymd(mut days: i64) -> (i64, i64, i64) {
    // Algorithm from http://howardhinnant.github.io/date_algorithms.html
    days += 719468;
    let era = if days >= 0 { days } else { days - 146096 } / 146097;
    let doe = days - era * 146097; // day of era [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365; // year of era [0, 399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // day of year [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = doy - (153 * mp + 2) / 5 + 1; // day [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 }; // month [1, 12]
    let y = if m <= 2 { y + 1 } else { y };
    (y, m, d)
}

pub(super) fn collect_projects() -> anyhow::Result<Vec<IndexedProject>> {
    let database_url = db::resolve_database_url()?;
    let mut conn = db::connect_readonly(&database_url)?;
    let mut seen_ids = std::collections::HashSet::new();
    let mut all = Vec::new();
    let rows = conn.query(
        "SELECT id,
                root_path,
                total_files::BIGINT AS total_files,
                total_symbols::BIGINT AS total_symbols,
                last_indexed_at::TEXT AS last_indexed_at,
                COALESCE(index_duration_ms, 0)::BIGINT AS index_duration_ms,
                NULL::BIGINT AS total_eligible_files
         FROM code_indexed_projects
         ORDER BY last_indexed_at DESC NULLS LAST",
        &[],
    )?;

    for row in rows {
        if let Ok(project) = indexed_project_from_row(&row)
            && seen_ids.insert(project.id.clone())
        {
            all.push(project);
        }
    }

    Ok(all)
}

pub(super) fn indexed_project_from_row(row: &postgres::Row) -> anyhow::Result<IndexedProject> {
    Ok(IndexedProject {
        id: row.try_get("id")?,
        root_path: row.try_get("root_path")?,
        total_files: row.try_get::<_, i64>("total_files")? as usize,
        total_symbols: row.try_get::<_, i64>("total_symbols")? as usize,
        last_indexed_at: row
            .try_get::<_, Option<String>>("last_indexed_at")?
            .unwrap_or_default(),
        index_duration_ms: row.try_get::<_, i64>("index_duration_ms")? as u64,
        total_eligible_files: row
            .try_get::<_, Option<i64>>("total_eligible_files")
            .ok()
            .flatten()
            .map(|n| n as usize),
    })
}

pub(super) fn format_coverage(indexed: usize, eligible: Option<usize>) -> String {
    match eligible {
        Some(total) if total > 0 => {
            let pct = (indexed as f64 / total as f64 * 100.0) as usize;
            format!("{indexed}/{total} ({pct}%)")
        }
        _ => format!("{indexed}"),
    }
}

pub(super) fn display_name(p: &IndexedProject) -> String {
    if p.root_path.is_empty() || !Path::new(&p.root_path).is_absolute() {
        return format!("<unknown> ({})", p.id);
    }
    let basename = Path::new(&p.root_path)
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_else(|| p.id.clone());
    format!("{basename} ({})", short_id(&p.id))
}
