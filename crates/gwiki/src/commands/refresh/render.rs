use super::*;

pub(crate) fn render_refresh(render: RefreshRender) -> CommandOutcome {
    let indexed = render.index_status.indexed.clone();
    let status = refresh_status(
        render.dry_run,
        render.refreshed.len(),
        render.unchanged.len(),
        render.failed.len(),
    );
    // Only an explicit single-source refresh should fail the command when every
    // attempted source failed; scheduled/bulk refreshes report failures in JSON.
    let exit_code = if render.explicit
        && !render.dry_run
        && !render.failed.is_empty()
        && render.refreshed.is_empty()
        && render.unchanged.is_empty()
    {
        1
    } else {
        0
    };
    let payload = json!({
        "command": "refresh",
        "scope": render.scope,
        "status": status,
        "dry_run": render.dry_run,
        "planned": render.planned,
        "refreshed": render.refreshed,
        "unchanged": render.unchanged,
        "failed": render.failed,
        "skipped": render.skipped,
        "indexed": indexed,
        "index_status": render.index_status,
        "degradations": render.degradations,
    });
    let text = format!(
        "Refresh sources\nScope: {}\nStatus: {status}\nPlanned: {}\nRefreshed: {}\nUnchanged: {}\nFailed: {}\nSkipped: {}",
        render.scope,
        render.planned.len(),
        render.refreshed.len(),
        render.unchanged.len(),
        render.failed.len(),
        render.skipped.len()
    );
    let mut outcome = crate::commands::scoped_outcome("refresh", &render.scope, payload, text);
    outcome.exit_code = exit_code;
    outcome
}

fn refresh_status(
    dry_run: bool,
    refreshed: usize,
    unchanged: usize,
    failed: usize,
) -> &'static str {
    if dry_run {
        "dry_run"
    } else if failed > 0 && refreshed == 0 && unchanged == 0 {
        "failed"
    } else if failed > 0 {
        "partial"
    } else if refreshed > 0 {
        "refreshed"
    } else {
        "unchanged"
    }
}
