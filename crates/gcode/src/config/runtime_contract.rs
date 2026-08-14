//! Revision-coherent PostgreSQL fallback for registered runtime configuration.

use std::collections::BTreeMap;

use anyhow::Context as _;
use postgres::{Client, IsolationLevel, error::SqlState};

#[derive(Debug)]
pub(super) struct HubConfigSnapshot {
    revision: i64,
    values: BTreeMap<String, String>,
}

impl HubConfigSnapshot {
    pub(super) fn revision(&self) -> i64 {
        self.revision
    }

    pub(super) fn value(&self, key: &str) -> Option<&str> {
        self.values.get(key).map(String::as_str)
    }
}

pub(super) fn capture_hub_snapshot(conn: &mut Client) -> anyhow::Result<HubConfigSnapshot> {
    capture_hub_snapshot_with_hook(conn, || {})
}

pub(super) fn capture_hub_snapshot_with_hook(
    conn: &mut Client,
    after_config_rows: impl FnOnce(),
) -> anyhow::Result<HubConfigSnapshot> {
    let mut transaction = conn
        .build_transaction()
        .isolation_level(IsolationLevel::RepeatableRead)
        .read_only(true)
        .start()
        .context("failed to start runtime configuration snapshot")?;
    let revision = transaction
        .query_one("SELECT revision FROM config_state WHERE id = true", &[])
        .context("failed to capture runtime configuration revision")?
        .try_get("revision")?;
    let rows = transaction
        .query("SELECT key, value FROM config_store ORDER BY key", &[])
        .context("failed to capture runtime configuration rows")?;
    let mut raw_values = BTreeMap::new();
    for row in rows {
        let key: String = row.try_get("key")?;
        if !gobby_core::config::is_registered_runtime_key(&key) {
            continue;
        }
        let raw: String = row.try_get("value")?;
        if let Some(value) = gobby_core::config::decode_config_value(&raw) {
            raw_values.insert(key, value);
        }
    }

    after_config_rows();

    let mut values = BTreeMap::new();
    for (key, value) in raw_values {
        if value.contains("${") {
            anyhow::bail!(
                "registered runtime configuration key {key:?} contains an environment reference"
            );
        }
        gobby_core::config::reject_secret_marker(&value)
            .with_context(|| format!("grant-issuance bug in runtime configuration key {key:?}"))?;
        values.insert(key, value);
    }
    transaction
        .commit()
        .context("failed to finish runtime configuration snapshot")?;
    Ok(HubConfigSnapshot { revision, values })
}

pub(super) fn hub_capture_permission_denied(error: &anyhow::Error) -> bool {
    error.chain().any(|cause| {
        cause
            .downcast_ref::<postgres::Error>()
            .and_then(postgres::Error::code)
            .is_some_and(|code| *code == SqlState::INSUFFICIENT_PRIVILEGE)
    })
}
