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
    /// Proven-dead native host epochs on this machine; use an empty set without evidence.
    /// Epochs are opaque, unique identities and death must be monotonic. A producer
    /// may snapshot this machine's native row epochs, then confirm the observed
    /// epoch is still live by ping, and supply the snapshot minus that epoch.
    /// Reversing that order could include a newly live epoch; a stale snapshot
    /// cannot authorize settlement of epochs absent from the snapshot.
    pub dead_host_epochs: std::collections::BTreeSet<String>,
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
                    // An omitted field cannot inherit the prior host's kill proof.
                    if let Some(process) = after.process.as_mut().and_then(Value::as_object_mut) {
                        for key in ["host_terminal_id", "pgid", "start_time"] {
                            process.remove(key);
                        }
                    }
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
            let dead_epoch = current.host_epoch.as_deref().is_some_and(|epoch| {
                epoch != inventory.epoch && options.dead_host_epochs.contains(epoch)
            });
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
                    match absent_process_status(effects, &current).await? {
                        ProcessStatus::Alive => return Ok(()),
                        ProcessStatus::Dead => after.state = State::Exited,
                        ProcessStatus::Unknown => after.state = State::Orphaned,
                    }
                }
                State::Orphaned if current.host_epoch.as_deref() == Some(&inventory.epoch) => {
                    after.state = State::Exited;
                }
                State::Live if dead_epoch => after.state = State::Orphaned,
                State::Orphaned if dead_epoch => {
                    if absent_process_status(effects, &current).await? != ProcessStatus::Dead {
                        return Ok(());
                    }
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

async fn absent_process_status(
    effects: &impl Effects,
    terminal: &Terminal,
) -> Result<ProcessStatus, ReconcileError> {
    if terminal
        .process
        .as_ref()
        .and_then(|process| process.get("pgid"))
        .is_none_or(Value::is_null)
    {
        return Ok(ProcessStatus::Dead);
    }
    timeout(RUNTIME_DEADLINE, effects.process_status(terminal))
        .await
        .map_err(|_| ReconcileError::RuntimeTimeout)?
        .map_err(Into::into)
}

/// How long a preserved input grant waits for its gclient to rebind (#23076).
pub const INPUT_HANDOFF_REBIND: Duration = Duration::from_secs(30);

/// Rebind deadlines per handoff record, carried by the caller across passes.
pub type HandoffDeadlines = BTreeMap<(Uuid, repository::InputHandoff), std::time::Instant>;

pub trait InputRevoke: Sync {
    /// Revoke `attachment_id`'s grant only while the host slot still holds it.
    fn revoke_input(
        &self,
        host_epoch: &str,
        host_terminal_id: &str,
        attachment_id: Uuid,
    ) -> impl Future<Output = anyhow::Result<()>> + Send;
}

pub async fn expire_input_handoffs(
    pool: &Pool,
    machine: Uuid,
    host_epoch: &str,
    deadlines: &mut HandoffDeadlines,
    now: std::time::Instant,
    effects: &impl InputRevoke,
) -> Result<(), ReconcileError> {
    let rows = pool
        .transaction(None, async |tx| {
            Ok::<_, ReconcileError>(repository::list_input_handoffs(tx, machine).await?)
        })
        .await?;
    let mut present = BTreeSet::new();
    for row in rows {
        let Some(handoff) = repository::InputHandoff::of(&row) else {
            continue;
        };
        let key = (row.id, handoff);
        present.insert(key.clone());
        let handoff = &key.1;
        let bound = handoff.host_epoch == host_epoch
            && row.host_epoch.as_deref() == Some(host_epoch)
            && row
                .locator
                .as_ref()
                .and_then(|locator| locator.get("host_terminal_id"))
                .and_then(Value::as_str)
                == Some(handoff.host_terminal_id.as_str());
        if bound {
            let deadline = *deadlines
                .entry(key.clone())
                .or_insert(now + INPUT_HANDOFF_REBIND);
            if now < deadline {
                continue;
            }
            let revoke =
                effects.revoke_input(host_epoch, &handoff.host_terminal_id, handoff.attachment_id);
            if !matches!(timeout(RUNTIME_DEADLINE, revoke).await, Ok(Ok(()))) {
                // The record keeps the authority discoverable; the next pass retries.
                continue;
            }
        }
        // An old host identity carries no authority into a new host. Only the
        // record read here is cleared, so a rebind or replacement that landed wins.
        pool.transaction(None, async |tx| {
            repository::clear_input_handoff(tx, row.id, Some(handoff)).await?;
            Ok::<_, ReconcileError>(())
        })
        .await?;
        deadlines.remove(&key);
    }
    deadlines.retain(|key, _| present.contains(key));
    Ok(())
}
