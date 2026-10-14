//! Durable terminal rows over the shared gcore transaction seam.

use chrono::{DateTime, Utc};
use gobby_core::postgres_pool::{Transaction, TransactionError};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use uuid::Uuid;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Backend {
    Native,
    Tmux,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Ownership {
    Gobby,
    External,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum State {
    Pending,
    Live,
    Exited,
    Orphaned,
}

impl State {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Pending => "pending",
            Self::Live => "live",
            Self::Exited => "exited",
            Self::Orphaned => "orphaned",
        }
    }
}

#[derive(Debug, thiserror::Error)]
pub enum RepositoryError {
    #[error(transparent)]
    Database(#[from] TransactionError),
    #[error("invalid terminal row encoding")]
    Encoding(#[from] serde_json::Error),
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Terminal {
    pub id: Uuid,
    pub backend: Backend,
    pub ownership: Ownership,
    pub state: State,
    pub spawn_key: Option<String>,
    pub machine_id: Uuid,
    pub project_id: Uuid,
    pub session_id: Option<Uuid>,
    pub agent_run_id: Option<Uuid>,
    pub locator: Option<Value>,
    pub locator_key: Option<String>,
    pub session_name: Option<String>,
    pub window_id: Option<String>,
    pub title: Option<String>,
    pub host_epoch: Option<String>,
    pub rows: Option<i32>,
    pub cols: Option<i32>,
    pub attempt_generation: i32,
    pub attempt_started_at: DateTime<Utc>,
    pub process: Option<Value>,
    pub unresolved_writes: Value,
    pub automatic_write_quarantined_at: Option<DateTime<Utc>>,
    pub automatic_write_quarantine_action_key: Option<String>,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    pub liveness_at: Option<DateTime<Utc>>,
}

/// Encode exactly the existing table's fields; PostgreSQL enforces its invariants.
pub async fn insert(tx: &Transaction<'_>, terminal: &Terminal) -> Result<(), RepositoryError> {
    tx.execute(
        "INSERT INTO terminals SELECT * FROM jsonb_populate_record(NULL::terminals, $1::text::jsonb)",
        &[&serde_json::to_string(terminal)?],
    )
    .await?;
    Ok(())
}

pub async fn get(tx: &Transaction<'_>, id: Uuid) -> Result<Option<Terminal>, RepositoryError> {
    read(tx, id, false).await
}

pub async fn get_for_update(
    tx: &Transaction<'_>,
    id: Uuid,
) -> Result<Option<Terminal>, RepositoryError> {
    read(tx, id, true).await
}

async fn read(
    tx: &Transaction<'_>,
    id: Uuid,
    lock: bool,
) -> Result<Option<Terminal>, RepositoryError> {
    let sql = if lock {
        "SELECT to_jsonb(t)::text AS terminal FROM terminals t WHERE id=$1::text::uuid FOR UPDATE"
    } else {
        "SELECT to_jsonb(t)::text AS terminal FROM terminals t WHERE id=$1::text::uuid"
    };
    tx.query_opt(sql, &[&id.to_string()])
        .await?
        .map(|row| {
            let encoded: String = row.try_get("terminal").map_err(TransactionError::from)?;
            Ok(serde_json::from_str(&encoded)?)
        })
        .transpose()
}

pub async fn list_reconcilable(
    tx: &Transaction<'_>,
    machine: Uuid,
) -> Result<Vec<Terminal>, RepositoryError> {
    tx.query(
        "SELECT to_jsonb(t)::text AS terminal FROM terminals t
         WHERE machine_id=$1::text::uuid AND backend='native'
         AND state IN ('pending','live','orphaned') ORDER BY created_at,id",
        &[&machine.to_string()],
    )
    .await?
    .into_iter()
    .map(|row| {
        let encoded: String = row.try_get("terminal").map_err(TransactionError::from)?;
        Ok(serde_json::from_str(&encoded)?)
    })
    .collect()
}

/// Write only settlement-owned columns, fenced by the captured row identity.
/// Callers hold `get_for_update` and commit paired run evidence through this same transaction.
pub(crate) async fn settle(
    tx: &Transaction<'_>,
    before: &Terminal,
    after: &Terminal,
) -> Result<bool, RepositoryError> {
    let locator = after
        .locator
        .as_ref()
        .map(serde_json::to_string)
        .transpose()?;
    let process = after
        .process
        .as_ref()
        .map(serde_json::to_string)
        .transpose()?;
    Ok(tx
        .execute(
            "UPDATE terminals SET state=$1, locator=$2::text::jsonb, locator_key=$3,
         host_epoch=$4, process=$5::text::jsonb, updated_at=$6::text::timestamptz
         WHERE id=$7::text::uuid AND machine_id=$8::text::uuid
         AND backend='native' AND attempt_generation=$9
         AND host_epoch IS NOT DISTINCT FROM $10
         AND attempt_started_at=$11::text::timestamptz
         AND spawn_key IS NOT DISTINCT FROM $12 AND state=$13",
            &[
                &after.state.as_str(),
                &locator,
                &after.locator_key,
                &after.host_epoch,
                &process,
                &after.updated_at.to_rfc3339(),
                &before.id.to_string(),
                &before.machine_id.to_string(),
                &before.attempt_generation,
                &before.host_epoch,
                &before.attempt_started_at.to_rfc3339(),
                &before.spawn_key,
                &before.state.as_str(),
            ],
        )
        .await?
        == 1)
}

const HANDOFF_KEY: &str = "native_input_handoff";

/// A daemon-shutdown input grant kept for the adopting daemon in `process`.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct InputHandoff {
    pub attachment_id: Uuid,
    pub host_epoch: String,
    pub host_terminal_id: String,
}

impl InputHandoff {
    pub fn of(terminal: &Terminal) -> Option<Self> {
        serde_json::from_value(terminal.process.as_ref()?.get(HANDOFF_KEY)?.clone()).ok()
    }
}

pub async fn record_input_handoff(
    tx: &Transaction<'_>,
    terminal: Uuid,
    machine: Uuid,
    handoff: &InputHandoff,
) -> Result<bool, RepositoryError> {
    Ok(tx
        .execute(
            "UPDATE terminals SET process = COALESCE(process, '{}'::jsonb)
             || jsonb_build_object('native_input_handoff', $1::text::jsonb), updated_at = now()
             WHERE id=$2::text::uuid AND machine_id=$3::text::uuid
             AND backend='native' AND state='live'
             AND host_epoch=$4 AND locator->>'host_terminal_id'=$5",
            &[
                &serde_json::to_string(handoff)?,
                &terminal.to_string(),
                &machine.to_string(),
                &handoff.host_epoch,
                &handoff.host_terminal_id,
            ],
        )
        .await?
        == 1)
}

pub async fn list_input_handoffs(
    tx: &Transaction<'_>,
    machine: Uuid,
) -> Result<Vec<Terminal>, RepositoryError> {
    // Rows settled since preservation stay listed until their record is retired.
    tx.query(
        "SELECT to_jsonb(t)::text AS terminal FROM terminals t
         WHERE machine_id=$1::text::uuid AND backend='native'
         AND process ? 'native_input_handoff' ORDER BY created_at,id",
        &[&machine.to_string()],
    )
    .await?
    .into_iter()
    .map(|row| {
        let encoded: String = row.try_get("terminal").map_err(TransactionError::from)?;
        Ok(serde_json::from_str(&encoded)?)
    })
    .collect()
}

pub async fn clear_input_handoff(
    tx: &Transaction<'_>,
    terminal: Uuid,
    expected: Option<&InputHandoff>,
) -> Result<bool, RepositoryError> {
    let expected = expected.map(serde_json::to_string).transpose()?;
    Ok(tx
        .execute(
            "UPDATE terminals SET process = process - 'native_input_handoff', updated_at = now()
             WHERE id=$1::text::uuid AND backend='native' AND process ? 'native_input_handoff'
             AND ($2::text IS NULL OR process->'native_input_handoff' = $2::text::jsonb)",
            &[&terminal.to_string(), &expected],
        )
        .await?
        == 1)
}
