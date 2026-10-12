//! Reconcile an accepted host inventory under its observed machine and epoch.

use std::{
    collections::{BTreeMap, BTreeSet},
    future::Future,
    time::Duration,
};

use chrono::{DateTime, Utc};
use gobby_core::postgres_pool::{Pool, Transaction};
use serde::Deserialize;
use serde_json::{Value, json};
use tokio::time::timeout;
use uuid::Uuid;

use crate::{
    control::Inventory,
    repository::{self, Backend, State, Terminal},
};

const RUNTIME_DEADLINE: Duration = Duration::from_secs(5);

#[derive(Debug, thiserror::Error)]
pub enum ReconcileError {
    #[error(transparent)]
    Database(#[from] gobby_core::postgres_pool::TransactionError),
    #[error(transparent)]
    Repository(#[from] repository::RepositoryError),
    #[error("invalid host inventory: {0}")]
    Inventory(String),
    #[error("terminal runtime operation timed out")]
    RuntimeTimeout,
    #[error("untracked host child could not be proven dead")]
    UnprovenKill,
    #[error("terminal runtime or paired run settlement failed")]
    Runtime(#[from] anyhow::Error),
}

#[derive(Debug, Clone, Copy)]
pub enum Observation<'a> {
    Accepted(&'a Inventory),
    Refused,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProcessStatus {
    Alive,
    Dead,
    Unknown,
}

#[derive(Debug, Clone)]
pub struct Options {
    pub now: DateTime<Utc>,
    pub spawn_in_doubt: Duration,
    pub settle_indeterminate: bool,
}

/// Runtime truth and run-family settlement share the row transaction.
/// Implementations write paired run evidence through `tx`, never a separate connection,
/// and reject a missing run or a run bound to a different machine/terminal. `kill`
/// must send the observed epoch to the host and return true only after proven death.
pub trait Effects: Sync {
    /// Local prepared spawns are settled exclusively by their current owner.
    fn holds(&self, _terminal_id: Uuid) -> bool {
        false
    }
    fn process_status(
        &self,
        terminal: &Terminal,
    ) -> impl Future<Output = anyhow::Result<ProcessStatus>> + Send;
    fn kill(
        &self,
        host_epoch: &str,
        host_terminal_id: &str,
    ) -> impl Future<Output = anyhow::Result<bool>> + Send;
    fn settle_run(
        &self,
        tx: &Transaction<'_>,
        before: &Terminal,
        after: &Terminal,
    ) -> impl Future<Output = anyhow::Result<()>> + Send;
}

#[derive(Debug, Clone, Copy, Default, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum CommitState {
    Prepared,
    #[default]
    Committed,
}

#[derive(Debug, Deserialize)]
struct HostEntry {
    spawn_key: String,
    host_terminal_id: Option<String>,
    #[serde(default)]
    commit_state: CommitState,
    pgid: Option<i32>,
    start_time: Option<Value>,
}

impl HostEntry {
    fn slot(&self, id: Uuid) -> String {
        self.host_terminal_id
            .clone()
            .unwrap_or_else(|| id.to_string())
    }

    fn merge_process(&self, row: &mut Terminal) {
        let mut process = row.process.clone().unwrap_or_else(|| json!({}));
        if let Some(map) = process.as_object_mut() {
            map.insert("host_terminal_id".into(), json!(self.slot(row.id)));
            if let Some(pgid) = self.pgid {
                map.insert("pgid".into(), json!(pgid));
                if let Some(start) = &self.start_time {
                    map.insert("start_time".into(), start.clone());
                }
            }
        }
        row.process = Some(process);
    }
}

fn inventory_entries(inventory: &Inventory) -> Result<BTreeMap<Uuid, HostEntry>, ReconcileError> {
    if inventory.epoch.is_empty() {
        return Err(ReconcileError::Inventory("empty host epoch".into()));
    }
    let mut entries = BTreeMap::new();
    let mut spawn_keys = BTreeSet::new();
    let mut host_slots = BTreeSet::new();
    for value in &inventory.terminals {
        let raw_id = value
            .get("terminal_id")
            .and_then(Value::as_str)
            .ok_or_else(|| ReconcileError::Inventory("missing terminal identity".into()))?;
        // Tmux observer identities are locator keys, not native child UUIDs.
        let Ok(id) = Uuid::parse_str(raw_id) else {
            continue;
        };
        let entry: HostEntry = serde_json::from_value(value.clone())
            .map_err(|error| ReconcileError::Inventory(error.to_string()))?;
        if entry.spawn_key.is_empty()
            || entry.host_terminal_id.as_deref() == Some("")
            || entry.pgid.is_some_and(|pgid| pgid <= 0)
        {
            return Err(ReconcileError::Inventory(
                "invalid native process identity".into(),
            ));
        }
        if !spawn_keys.insert(entry.spawn_key.clone()) || !host_slots.insert(entry.slot(id)) {
            return Err(ReconcileError::Inventory(
                "duplicate spawn key or host slot".into(),
            ));
        }
        if entries.insert(id, entry).is_some() {
            return Err(ReconcileError::Inventory(format!(
                "duplicate durable identity {id}"
            )));
        }
    }
    Ok(entries)
}

fn same_attempt(snapshot: &Terminal, current: &Terminal) -> bool {
    snapshot.id == current.id
        && snapshot.backend == current.backend
        && snapshot.ownership == current.ownership
        && snapshot.machine_id == current.machine_id
        && snapshot.project_id == current.project_id
        && snapshot.session_id == current.session_id
        && snapshot.agent_run_id == current.agent_run_id
        && snapshot.host_epoch == current.host_epoch
        && snapshot.spawn_key == current.spawn_key
        && snapshot.state == current.state
        && snapshot.attempt_generation == current.attempt_generation
        && snapshot.attempt_started_at == current.attempt_started_at
}

async fn kill(effects: &impl Effects, epoch: &str, slot: &str) -> Result<bool, ReconcileError> {
    Ok(timeout(RUNTIME_DEADLINE, effects.kill(epoch, slot))
        .await
        .map_err(|_| ReconcileError::RuntimeTimeout)??)
}

async fn kill_untracked(
    effects: &impl Effects,
    epoch: &str,
    slot: &str,
) -> Result<(), ReconcileError> {
    if kill(effects, epoch, slot).await? {
        Ok(())
    } else {
        Err(ReconcileError::UnprovenKill)
    }
}

async fn persist(
    tx: &Transaction<'_>,
    before: &Terminal,
    after: &Terminal,
    effects: &impl Effects,
) -> Result<(), ReconcileError> {
    if before != after && repository::settle(tx, before, after).await? {
        effects.settle_run(tx, before, after).await?;
    }
    Ok(())
}

/// Rejected adoption never reads rows or invokes runtime effects. Accepted inventories
/// settle each captured attempt in its own locked transaction; replay performs no writes.
pub async fn reconcile(
    pool: &Pool,
    machine: Uuid,
    observation: Observation<'_>,
    options: &Options,
    effects: &impl Effects,
) -> Result<(), ReconcileError> {
    let Observation::Accepted(inventory) = observation else {
        return Ok(());
    };
    let entries = inventory_entries(inventory)?;
    let (rows, snapshots) = pool
        .transaction(None, async |tx| {
            let rows = repository::list_reconcilable(tx, machine).await?;
            let mut snapshots = BTreeMap::new();
            for id in entries.keys() {
                snapshots.insert(*id, repository::get(tx, *id).await?);
            }
            Ok::<_, ReconcileError>((rows, snapshots))
        })
        .await?;

    for (id, entry) in &entries {
        if effects.holds(*id) {
            continue;
        }
        pool.transaction(None, async |tx| {
            let current = repository::get_for_update(tx, *id).await?;
            let snapshot = snapshots.get(id).and_then(Option::as_ref);
            match (snapshot, current.as_ref()) {
                (Some(before), Some(now)) if !same_attempt(before, now) => return Ok(()),
                (None, Some(now)) if matches!(now.state, State::Pending | State::Live) => {
                    return Ok(());
                }
                (Some(_), None) => return Ok(()),
                _ => {}
            }
            if effects.holds(*id) {
                return Ok(());
            }
            let Some(current) = current else {
                kill_untracked(effects, &inventory.epoch, &entry.slot(*id)).await?;
                return Ok(());
            };
            // A row rebound to a newer host is outside this observation's authority.
            if current.machine_id == machine
                && current
                    .host_epoch
                    .as_deref()
                    .is_some_and(|epoch| epoch != inventory.epoch)
            {
                return Ok(());
            }
            if current.machine_id != machine
                || current.backend != Backend::Native
                || current.spawn_key.as_deref() != Some(&entry.spawn_key)
            {
                kill_untracked(effects, &inventory.epoch, &entry.slot(*id)).await?;
                return Ok(());
            }
            let mut after = current.clone();
            match (current.state, entry.commit_state) {
                (State::Pending, CommitState::Committed) => {
                    after.state = State::Live;
                    after.host_epoch = Some(inventory.epoch.clone());
                    after.locator = Some(json!({"host_terminal_id": entry.slot(*id)}));
                    after.locator_key =
                        Some(format!("native:{}:{}", inventory.epoch, entry.slot(*id)));
                    entry.merge_process(&mut after);
                }
                (State::Pending, CommitState::Prepared) => {
                    entry.merge_process(&mut after);
                    if options.settle_indeterminate
                        && kill(effects, &inventory.epoch, &entry.slot(*id)).await?
                    {
                        after.state = State::Exited;
                    }
                }
                (State::Orphaned, _) => {
                    after.host_epoch = Some(inventory.epoch.clone());
                    after.locator = Some(json!({"host_terminal_id": entry.slot(*id)}));
                    after.locator_key =
                        Some(format!("native:{}:{}", inventory.epoch, entry.slot(*id)));
                    entry.merge_process(&mut after);
                }
                _ => return Ok(()),
            }
            if after != current {
                after.updated_at = options.now;
                persist(tx, &current, &after, effects).await?;
            }
            Ok::<_, ReconcileError>(())
        })
        .await?;
    }

    for snapshot in rows {
        if effects.holds(snapshot.id)
            || entries
                .get(&snapshot.id)
                .is_some_and(|entry| snapshot.spawn_key.as_deref() == Some(&entry.spawn_key))
        {
            continue;
        }
        pool.transaction(None, async |tx| {
            let Some(current) = repository::get_for_update(tx, snapshot.id).await? else {
                return Ok(());
            };
            if !same_attempt(&snapshot, &current)
                || effects.holds(current.id)
                || current.machine_id != machine
                || current.backend != Backend::Native
            {
                return Ok(());
            }
            let mut after = current.clone();
            match current.state {
                State::Pending => {
                    let age = options
                        .now
                        .signed_duration_since(current.attempt_started_at);
                    if !options.settle_indeterminate
                        && (options.now < current.attempt_started_at
                            || age.to_std().unwrap_or_default() < options.spawn_in_doubt)
                    {
                        return Ok(());
                    }
                    after.state = State::Exited;
                }
                State::Live if current.host_epoch.as_deref() == Some(&inventory.epoch) => {
                    match timeout(RUNTIME_DEADLINE, effects.process_status(&current))
                        .await
                        .map_err(|_| ReconcileError::RuntimeTimeout)??
                    {
                        ProcessStatus::Alive => return Ok(()),
                        ProcessStatus::Dead => after.state = State::Exited,
                        ProcessStatus::Unknown => after.state = State::Orphaned,
                    }
                }
                State::Orphaned if current.host_epoch.as_deref() == Some(&inventory.epoch) => {
                    after.state = State::Exited;
                }
                _ => return Ok(()),
            }
            after.updated_at = options.now;
            persist(tx, &current, &after, effects).await?;
            Ok::<_, ReconcileError>(())
        })
        .await?;
    }
    Ok(())
}
