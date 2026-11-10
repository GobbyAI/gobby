//! Reads `project_policy.fleet_health` from the installed rule rows, the same authority
//! that `installed_project_policy` reads on the Python side. The watcher runs without the
//! Python daemon, so it queries `rule_definitions` through the bootstrap DSN. That DSN
//! carries the daemon's `search_path`, so the unqualified table name resolves the same way.

use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use anyhow::Context;
use serde_json::Value;

use super::policy::Thresholds;

const LOAD_TIMEOUT: Duration = Duration::from_secs(5);
/// Server-side bound on the read, below `LOAD_TIMEOUT` so the connection ends on its own.
const READ_STATEMENT_TIMEOUT_SQL: &str = "SET statement_timeout = '4s'";

/// The single outstanding policy read. A read that outlives its timeout keeps its thread
/// parked, so the next refresh must wait for this one to finish rather than park another.
static READ_IN_FLIGHT: AtomicBool = AtomicBool::new(false);

/// Holds the read slot until dropped. The blocking read owns it, so the slot frees when the
/// thread ends, even when the caller stopped waiting at `LOAD_TIMEOUT`.
pub struct Claim {
    flag: &'static AtomicBool,
}

impl Drop for Claim {
    fn drop(&mut self) {
        self.flag.store(false, Ordering::Release);
    }
}

/// Claims the read slot, or `None` while another read still holds it.
pub fn claim(flag: &'static AtomicBool) -> Option<Claim> {
    flag.compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
        .ok()
        .map(|_| Claim { flag })
}

/// Global rows plus the named project's rows, the same scope `list_effective` resolves. A
/// NULL project id (no project named) matches only the global rows.
const RULE_ROWS_SQL: &str = "SELECT name, source, enabled, definition_json::text \
     FROM rule_definitions \
     WHERE deleted_at IS NULL AND (project_id IS NULL OR project_id = $1::text::uuid)";

/// One `rule_definitions` row, reduced to the columns policy resolution reads.
#[derive(Debug, Clone, PartialEq)]
pub struct RuleRow {
    pub name: String,
    pub source: String,
    pub enabled: bool,
    pub definition: Value,
}

/// The `value` of each `set_variable` effect that writes `project_policy`. A missing value
/// yields `None`, which resolution treats as malformed.
fn project_policy_writes(definition: &Value) -> impl Iterator<Item = Option<&Value>> {
    definition
        .get("effects")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter(|effect| {
            effect.get("type").and_then(Value::as_str) == Some("set_variable")
                && effect.get("variable").and_then(Value::as_str) == Some("project_policy")
        })
        .map(|effect| effect.get("value"))
}

fn is_writer(row: &RuleRow) -> bool {
    project_policy_writes(&row.definition).next().is_some()
}

/// Resolve the single `project_policy` value and its `fleet_health` block.
///
/// Mirrors `installed_project_policy`: a writer that is not installed, a non-installed row
/// that shares a name with an installed writer, enabled writers that disagree, or a
/// malformed block are errors. `None` means no enabled writer or no `fleet_health` block, so
/// the caller reports the compiled defaults as their source.
pub fn thresholds_from_rule_rows(rows: &[RuleRow]) -> Result<Option<Thresholds>, String> {
    let installed_writer_names: Vec<&str> = rows
        .iter()
        .filter(|row| is_writer(row) && row.source == "installed")
        .map(|row| row.name.as_str())
        .collect();
    let uninstalled_writer = rows
        .iter()
        .any(|row| is_writer(row) && row.source != "installed");
    let shadowed = rows.iter().any(|row| {
        row.source != "installed" && installed_writer_names.contains(&row.name.as_str())
    });
    if uninstalled_writer || shadowed {
        return Err("project_policy writer is not installed".into());
    }

    let mut policies: Vec<&Value> = Vec::new();
    for row in rows.iter().filter(|row| row.enabled) {
        for value in project_policy_writes(&row.definition) {
            match value {
                Some(value) if value.is_object() => policies.push(value),
                _ => return Err("project_policy value must be an object".into()),
            }
        }
    }
    let Some((first, rest)) = policies.split_first() else {
        return Ok(None);
    };
    if rest.iter().any(|policy| policy != first) {
        return Err("enabled project_policy writers disagree".into());
    }
    match first.get("fleet_health") {
        Some(block) => Thresholds::from_fleet_health(block).map(Some),
        None => Ok(None),
    }
}

/// Query `rule_definitions` and resolve the thresholds. Any failure is an error, never
/// defaults: the caller decides whether a last-good value applies.
pub async fn load_thresholds(
    database_url: &str,
    project_id: Option<&str>,
) -> Result<Option<Thresholds>, String> {
    let Some(slot) = claim(&READ_IN_FLIGHT) else {
        return Err("a previous rule_definitions read is still outstanding".into());
    };
    let database_url = database_url.to_owned();
    let project_id = project_id.map(str::to_owned);
    let read = tokio::task::spawn_blocking(move || {
        // Held until this blocking read ends, including after the caller has timed out.
        let _slot = slot;
        read_rule_rows(&database_url, project_id.as_deref())
    });
    let joined = tokio::time::timeout(LOAD_TIMEOUT, read)
        .await
        .map_err(|_| "rule_definitions read timed out".to_string())?;
    let rows = joined
        .map_err(|error| format!("rule_definitions reader stopped: {error}"))?
        .map_err(|error| format!("{error:#}"))?;
    thresholds_from_rule_rows(&rows)
}

/// Blocking read-only query. Runs on the blocking pool because the gcore connection is sync.
fn read_rule_rows(database_url: &str, project_id: Option<&str>) -> anyhow::Result<Vec<RuleRow>> {
    let mut client = gobby_core::postgres::connect_readonly(database_url)?;
    client
        .batch_execute(READ_STATEMENT_TIMEOUT_SQL)
        .context("failed to bound the rule_definitions read")?;
    client
        .query(RULE_ROWS_SQL, &[&project_id])
        .context("failed to read rule_definitions")?
        .into_iter()
        .map(|row| {
            // `try_get` reports a column type mismatch as an error instead of panicking.
            let name = row.try_get(0).context("rule_definitions.name")?;
            let source = row.try_get(1).context("rule_definitions.source")?;
            let enabled = row.try_get(2).context("rule_definitions.enabled")?;
            let definition: String = row.try_get(3).context("rule_definitions.definition_json")?;
            Ok(RuleRow {
                name,
                source,
                enabled,
                definition: serde_json::from_str(&definition)
                    .context("rule definition is not JSON")?,
            })
        })
        .collect()
}

#[cfg(test)]
#[path = "policy_source/tests.rs"]
mod tests;
