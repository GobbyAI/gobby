use std::path::Path;

use serde_json::json;

use crate::collect::CollectReport;
use crate::{CommandOutcome, ScopeIdentity};

pub fn run(scope: ScopeIdentity, root: &Path, report: CollectReport) -> CommandOutcome {
    let accepted = report.accepted.len();
    let skipped = report.skipped.len();
    let payload = json!({
        "command": "collect",
        "scope": scope,
        "status": "ready",
        "root": root.display().to_string(),
        "accepted": accepted,
        "skipped": skipped,
        "actions": report,
    });
    let text = format!(
        "Collect ready\nScope: {scope}\nRoot: {}\nAccepted: {accepted}\nSkipped: {skipped}",
        root.display()
    );
    super::scoped_outcome("collect", &scope, payload, text)
}
