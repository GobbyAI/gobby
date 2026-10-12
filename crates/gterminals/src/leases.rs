//! Connection-scoped terminal lease state and ordered lifecycle publication.
pub use crate::repository::Backend;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use tokio::sync::{Mutex, mpsc, watch};
use uuid::Uuid;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Viewer {
    Web,
    Gclient,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Attachment {
    pub terminal_id: Uuid,
    pub id: Uuid,
    pub generation: u64,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AttachmentView {
    pub viewport: Option<(u16, u16)>,
    pub scroll_offset: u64,
}
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct TerminalView {
    pub holder: Option<Uuid>,
    pub generation: u64,
    pub sizing_owner: Option<Uuid>,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Sizing {
    pub applied: bool,
    pub owner: Option<Viewer>,
    pub rows: Option<u16>,
    pub cols: Option<u16>,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Control {
    pub granted: bool,
    pub reason: Option<String>,
    pub generation: u64,
    pub displaced: Option<Uuid>,
    pub sizing: Option<Sizing>,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Finalized {
    pub generation: u64,
    pub holder_lost: bool,
    pub sizing: Option<Sizing>,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScrollApplied {
    pub applied_rows: u64,
    pub max_rows: u64,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WriteAdmit {
    New,
    JoinInflight,
    Replay {
        outcome: String,
        reason: Option<String>,
    },
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LifecycleEvent {
    pub daemon_epoch: Uuid,
    pub lifecycle_seq: u64,
    pub terminal_id: Uuid,
    pub attachment_id: Uuid,
    pub kind: String,
    pub generation: u64,
    pub holder: Option<Uuid>,
    pub reason: Option<String>,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PublicationFault {
    pub connections: BTreeSet<Uuid>,
}
#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("stale attachment")]
    StaleAttachment,
    #[error("stale lease generation")]
    StaleGeneration,
    #[error("held")]
    Held,
    #[error("write sequence conflict")]
    WriteConflict,
    #[error("write sequence expired")]
    WriteExpired,
    #[error("write sequence capacity")]
    WriteCapacity,
    #[error("invalid dimensions")]
    InvalidDimensions,
    #[error("safe integer overflow")]
    SafeIntegerOverflow,
    #[error("lifecycle publication fault")]
    PublicationFault,
}

/// Connection leases and a bounded, ordered publication stream.
///
/// One mutex serializes terminal transitions with the shared event sequence.
/// After acquiring it, operations do no I/O or awaits: reservation, state change,
/// and publication cannot be interrupted by cancellation. WS owners subscribe to
/// `subscribe_fault` and close the connections named by the sticky fault signal.
pub struct Registry {
    epoch: Uuid,
    events: mpsc::Sender<LifecycleEvent>,
    fault: watch::Sender<Option<PublicationFault>>,
    state: Mutex<RegistryState>,
}

const MAX_SAFE_INTEGER: u64 = (1 << 53) - 1;
const WRITE_CAPACITY: usize = 64;
const MAX_CELLS: i64 = (2 * 1024 * 1024 - 64) / 96;

#[derive(Default)]
struct RegistryState {
    terminals: BTreeMap<Uuid, Terminal>,
    sequence: u64,
    attachment_generation: u64,
    faulted: bool,
}
#[derive(Default)]
struct Terminal {
    view: TerminalView,
    records: BTreeMap<Uuid, Record>,
    resize_sequence: u64,
}
struct Record {
    token: Attachment,
    connection: Uuid,
    viewer: Viewer,
    backend: Backend,
    finalized: bool,
    geometry: Option<(u16, u16)>,
    resize_sequence: u64,
    view: AttachmentView,
    writes: BTreeMap<u64, Write>,
    high_water: Option<u64>,
}
struct Write {
    fingerprint: [u8; 32],
    completion: Option<(String, Option<String>)>,
}

fn increment(value: u64) -> Result<u64, Error> {
    if value >= MAX_SAFE_INTEGER {
        Err(Error::SafeIntegerOverflow)
    } else {
        Ok(value + 1)
    }
}
fn dimensions(rows: i64, cols: i64) -> Result<(u16, u16), Error> {
    if !(1..=1024).contains(&rows) || !(1..=1024).contains(&cols) || rows * cols > MAX_CELLS {
        return Err(Error::InvalidDimensions);
    }
    Ok((rows as u16, cols as u16))
}
impl Terminal {
    fn record(&self, token: Attachment, allow_finalized: bool) -> Result<&Record, Error> {
        self.records
            .get(&token.id)
            .filter(|r| r.token == token && (allow_finalized || !r.finalized))
            .ok_or(Error::StaleAttachment)
    }
    fn record_mut(&mut self, token: Attachment) -> Result<&mut Record, Error> {
        self.records
            .get_mut(&token.id)
            .filter(|r| r.token == token && !r.finalized)
            .ok_or(Error::StaleAttachment)
    }
    fn elect(&mut self) -> Option<Sizing> {
        let old = self.view.sizing_owner;
        let sized: Vec<_> = self
            .records
            .values()
            .filter(|r| !r.finalized && r.geometry.is_some())
            .collect();
        let unattended = self.view.holder.is_none()
            && sized
                .iter()
                .all(|r| r.viewer == Viewer::Web && r.backend == Backend::Native);
        self.view.sizing_owner = sized
            .into_iter()
            .filter(|r| {
                unattended || r.viewer != Viewer::Web || self.view.holder == Some(r.token.id)
            })
            .max_by_key(|r| {
                (
                    if r.viewer == Viewer::Web { 2 } else { 1 },
                    r.resize_sequence,
                )
            })
            .map(|r| r.token.id);
        (old != self.view.sizing_owner).then(|| self.sizing(self.view.sizing_owner))
    }
    fn sizing(&self, requested: Option<Uuid>) -> Sizing {
        let owner = self.view.sizing_owner.and_then(|id| self.records.get(&id));
        let applied = requested.is_some() && requested == self.view.sizing_owner;
        let geometry = owner.filter(|_| applied).and_then(|r| r.geometry);
        Sizing {
            applied,
            owner: owner.map(|r| r.viewer),
            rows: geometry.map(|g| g.0),
            cols: geometry.map(|g| g.1),
        }
    }
}
impl RegistryState {
    fn healthy(&mut self, registry: &Registry, connection: Option<Uuid>) -> Result<(), Error> {
        if registry.events.is_closed() {
            registry.fault_locked(self, connection);
        }
        if self.faulted {
            Err(Error::PublicationFault)
        } else {
            Ok(())
        }
    }
    fn terminal(&self, token: Attachment) -> Result<&Terminal, Error> {
        self.terminals
            .get(&token.terminal_id)
            .ok_or(Error::StaleAttachment)
    }
    fn terminal_mut(&mut self, token: Attachment) -> Result<&mut Terminal, Error> {
        self.terminals
            .get_mut(&token.terminal_id)
            .ok_or(Error::StaleAttachment)
    }
}
impl Registry {
    /// Create a daemon epoch with a bounded publication queue.
    ///
    /// # Panics
    /// Panics when `capacity` is zero, which cannot hold a lifecycle transition.
    pub fn new(capacity: usize) -> (Self, mpsc::Receiver<LifecycleEvent>) {
        let (events, rx) = mpsc::channel(capacity);
        let (fault, _) = watch::channel(None);
        (
            Self {
                epoch: Uuid::new_v4(),
                events,
                fault,
                state: Mutex::new(RegistryState::default()),
            },
            rx,
        )
    }
    pub fn epoch(&self) -> Uuid {
        self.epoch
    }
    pub fn subscribe_fault(&self) -> watch::Receiver<Option<PublicationFault>> {
        self.fault.subscribe()
    }
    fn fault_locked(&self, state: &mut RegistryState, connection: Option<Uuid>) {
        if state.faulted {
            return;
        }
        state.faulted = true;
        let mut connections: BTreeSet<_> = state
            .terminals
            .values()
            .flat_map(|t| t.records.values())
            .filter(|r| !r.finalized)
            .map(|r| r.connection)
            .collect();
        connections.extend(connection);
        self.fault
            .send_replace(Some(PublicationFault { connections }));
    }
    fn reserve<'a>(
        &'a self,
        state: &mut RegistryState,
        connection: Option<Uuid>,
    ) -> Result<mpsc::Permit<'a, LifecycleEvent>, Error> {
        state.healthy(self, connection)?;
        match self.events.try_reserve() {
            Ok(permit) => Ok(permit),
            Err(_) => {
                self.fault_locked(state, connection);
                Err(Error::PublicationFault)
            }
        }
    }
    fn event(
        &self,
        token: Attachment,
        sequence: u64,
        kind: &str,
        view: &TerminalView,
        reason: Option<&str>,
    ) -> LifecycleEvent {
        LifecycleEvent {
            daemon_epoch: self.epoch,
            lifecycle_seq: sequence,
            terminal_id: token.terminal_id,
            attachment_id: token.id,
            kind: kind.into(),
            generation: view.generation,
            holder: view.holder,
            reason: reason.map(str::to_owned),
        }
    }
    pub async fn attach(
        &self,
        terminal: Uuid,
        connection: Uuid,
        viewer: Viewer,
        backend: Backend,
    ) -> Result<Attachment, Error> {
        let mut state = self.state.lock().await;
        state.healthy(self, Some(connection))?;
        let generation = increment(state.attachment_generation)?;
        let sequence = increment(state.sequence)?;
        let permit = self.reserve(&mut state, Some(connection))?;
        let token = Attachment {
            terminal_id: terminal,
            id: Uuid::new_v4(),
            generation,
        };
        let t = state.terminals.entry(terminal).or_default();
        t.records.insert(
            token.id,
            Record {
                token,
                connection,
                viewer,
                backend,
                finalized: false,
                geometry: None,
                resize_sequence: 0,
                view: AttachmentView {
                    viewport: None,
                    scroll_offset: 0,
                },
                writes: BTreeMap::new(),
                high_water: None,
            },
        );
        let event = self.event(token, sequence, "attached", &t.view, None);
        state.attachment_generation = generation;
        state.sequence = sequence;
        permit.send(event);
        Ok(token)
    }
    pub async fn take_control(
        &self,
        attachment: Attachment,
        takeover: bool,
    ) -> Result<Control, Error> {
        let mut state = self.state.lock().await;
        state.healthy(self, None)?;
        let t = state.terminal(attachment)?;
        t.record(attachment, false)?;
        if t.view.holder == Some(attachment.id) || (t.view.holder.is_some() && !takeover) {
            return Ok(Control {
                granted: t.view.holder == Some(attachment.id),
                reason: (t.view.holder != Some(attachment.id)).then(|| "held".into()),
                generation: t.view.generation,
                displaced: None,
                sizing: None,
            });
        }
        let generation = increment(t.view.generation)?;
        let sequence = increment(state.sequence)?;
        let permit = self.reserve(&mut state, None)?;
        let t = state.terminal_mut(attachment)?;
        let displaced = t.view.holder.replace(attachment.id);
        t.view.generation = generation;
        let sizing = t.elect();
        let event = self.event(attachment, sequence, "control_taken", &t.view, None);
        state.sequence = sequence;
        permit.send(event);
        Ok(Control {
            granted: true,
            reason: None,
            generation,
            displaced,
            sizing,
        })
    }
    pub async fn release_control(&self, attachment: Attachment) -> Result<Control, Error> {
        let mut state = self.state.lock().await;
        state.healthy(self, None)?;
        let t = state.terminal(attachment)?;
        t.record(attachment, false)?;
        if t.view.holder != Some(attachment.id) {
            return Ok(Control {
                granted: false,
                reason: Some("released".into()),
                generation: t.view.generation,
                displaced: None,
                sizing: None,
            });
        }
        let generation = increment(t.view.generation)?;
        let sequence = increment(state.sequence)?;
        let permit = self.reserve(&mut state, None)?;
        let t = state.terminal_mut(attachment)?;
        t.view.holder = None;
        t.view.generation = generation;
        let sizing = t.elect();
        let event = self.event(
            attachment,
            sequence,
            "control_released",
            &t.view,
            Some("released"),
        );
        state.sequence = sequence;
        permit.send(event);
        Ok(Control {
            granted: false,
            reason: Some("released".into()),
            generation,
            displaced: None,
            sizing,
        })
    }
    pub async fn finalize(
        &self,
        attachment: Attachment,
        reason: &str,
    ) -> Result<Option<Finalized>, Error> {
        let mut state = self.state.lock().await;
        state.healthy(self, None)?;
        let t = state.terminal(attachment)?;
        if t.record(attachment, true)?.finalized {
            return Ok(None);
        }
        let holder_lost = t.view.holder == Some(attachment.id);
        let generation = if holder_lost {
            increment(t.view.generation)?
        } else {
            t.view.generation
        };
        let sequence = increment(state.sequence)?;
        let permit = self.reserve(&mut state, None)?;
        let t = state.terminal_mut(attachment)?;
        if holder_lost {
            t.view.holder = None;
        }
        t.view.generation = generation;
        let r = t.record_mut(attachment)?;
        r.finalized = true;
        r.writes.clear();
        let sizing = t.elect();
        let sizing = if t.records.values().all(|r| r.finalized) {
            Some(t.sizing(None))
        } else {
            sizing
        };
        let event = self.event(attachment, sequence, "finalized", &t.view, Some(reason));
        state.sequence = sequence;
        permit.send(event);
        Ok(Some(Finalized {
            generation,
            holder_lost,
            sizing,
        }))
    }
    pub async fn snapshot(&self, terminal: Uuid) -> TerminalView {
        self.state
            .lock()
            .await
            .terminals
            .get(&terminal)
            .map(|t| t.view.clone())
            .unwrap_or_default()
    }
    pub async fn attachment(&self, attachment: Attachment) -> Result<AttachmentView, Error> {
        let mut state = self.state.lock().await;
        state.healthy(self, None)?;
        Ok(state
            .terminal(attachment)?
            .record(attachment, false)?
            .view
            .clone())
    }
    pub async fn resize(
        &self,
        attachment: Attachment,
        rows: i64,
        cols: i64,
    ) -> Result<Sizing, Error> {
        let mut state = self.state.lock().await;
        state.healthy(self, None)?;
        let t = state.terminal_mut(attachment)?;
        t.record(attachment, false)?;
        let geometry = dimensions(rows, cols)?;
        let sequence = increment(t.resize_sequence)?;
        let r = t.record_mut(attachment)?;
        r.geometry = Some(geometry);
        r.resize_sequence = sequence;
        t.resize_sequence = sequence;
        t.elect();
        Ok(t.sizing(Some(attachment.id)))
    }
    pub async fn set_viewport(
        &self,
        attachment: Attachment,
        rows: i64,
        cols: i64,
    ) -> Result<(u16, u16), Error> {
        let mut state = self.state.lock().await;
        state.healthy(self, None)?;
        let r = state.terminal_mut(attachment)?.record_mut(attachment)?;
        let geometry = dimensions(rows, cols)?;
        r.view.viewport = Some(geometry);
        Ok(geometry)
    }
    pub async fn set_scroll(
        &self,
        attachment: Attachment,
        rows: i64,
        max: i64,
    ) -> Result<ScrollApplied, Error> {
        let mut state = self.state.lock().await;
        state.healthy(self, None)?;
        let r = state.terminal_mut(attachment)?.record_mut(attachment)?;
        if rows.unsigned_abs() > MAX_SAFE_INTEGER || max.unsigned_abs() > MAX_SAFE_INTEGER {
            return Err(Error::SafeIntegerOverflow);
        }
        let max_rows = max.max(0) as u64;
        let applied_rows = (rows.max(0) as u64).min(max_rows);
        r.view.scroll_offset = applied_rows;
        Ok(ScrollApplied {
            applied_rows,
            max_rows,
        })
    }
    pub async fn admit_write(
        &self,
        attachment: Attachment,
        expected_generation: u64,
        seq: u64,
        kind: &str,
        payload: &[u8],
    ) -> Result<WriteAdmit, Error> {
        let mut state = self.state.lock().await;
        state.healthy(self, None)?;
        let t = state.terminal_mut(attachment)?;
        t.record(attachment, false)?;
        if seq > MAX_SAFE_INTEGER || expected_generation > MAX_SAFE_INTEGER {
            return Err(Error::SafeIntegerOverflow);
        }
        if t.view.holder != Some(attachment.id) {
            return Err(Error::Held);
        }
        if t.view.generation != expected_generation {
            return Err(Error::StaleGeneration);
        }
        let r = t.record_mut(attachment)?;
        let mut hash = Sha256::new();
        hash.update(kind.as_bytes());
        hash.update(b":");
        hash.update(payload);
        let fingerprint: [u8; 32] = hash.finalize().into();
        if let Some(write) = r.writes.get(&seq) {
            if write.fingerprint != fingerprint {
                return Err(Error::WriteConflict);
            }
            return Ok(match &write.completion {
                None => WriteAdmit::JoinInflight,
                Some((outcome, reason)) => WriteAdmit::Replay {
                    outcome: outcome.clone(),
                    reason: reason.clone(),
                },
            });
        }
        if r.high_water.is_some_and(|high| seq <= high) {
            return Err(Error::WriteExpired);
        }
        if r.writes.len() >= WRITE_CAPACITY {
            if r.writes
                .first_key_value()
                .is_some_and(|(_, w)| w.completion.is_none())
            {
                return Err(Error::WriteCapacity);
            }
            r.writes.pop_first();
        }
        r.high_water = Some(seq);
        r.writes.insert(
            seq,
            Write {
                fingerprint,
                completion: None,
            },
        );
        Ok(WriteAdmit::New)
    }
    pub async fn complete_write(
        &self,
        attachment: Attachment,
        seq: u64,
        outcome: &str,
        reason: Option<&str>,
    ) -> Result<(), Error> {
        let mut state = self.state.lock().await;
        state.healthy(self, None)?;
        let r = state.terminal_mut(attachment)?.record_mut(attachment)?;
        if seq > MAX_SAFE_INTEGER {
            return Err(Error::SafeIntegerOverflow);
        }
        if let Some(write) = r.writes.get_mut(&seq)
            && write.completion.is_none()
        {
            write.completion = Some((outcome.into(), reason.map(str::to_owned)));
        }
        Ok(())
    }
}
