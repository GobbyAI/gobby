//! Daemon work the live loop runs beside itself (plan A1 of
//! gclient-daemon-resilience). A job is a spawned future whose outcome comes
//! back on one channel the loop selects on, so no daemon round trip ever sits
//! between two frames. Each key keeps one job in flight and the latest
//! wish pending; the ledger tags every job with the connection generation
//! it was issued on, and only the job a key still waits for settles it.

use std::collections::{HashMap, HashSet};
use std::future::Future;
use std::hash::Hash;

use ratatui::backend::Backend;
use ratatui::Terminal;
use tokio::sync::mpsc::{self, UnboundedReceiver, UnboundedSender};

use crate::daemon::{Daemon, DaemonError, Generation, LiveDaemon, WorkspaceOp};
use crate::frame_source::FrameError;
use crate::ui::status::Toast;
use crate::ui::Chrome;

use super::super::{PaneId, Workspace};
use super::focus_hints::{self, FocusMemo, ShownFocus};

#[cfg(test)]
mod tests;

/// What a job is about. One job per key is in flight at a time.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum JobKey {
    Geometry(PaneId),
    FocusHints,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JobTag {
    pub id: u64,
    pub generation: Generation,
    pub key: JobKey,
}

#[derive(Debug)]
pub struct JobOutcome {
    pub tag: JobTag,
    pub result: JobResult,
}

/// What a workspace op was sent for, so its outcome lands where it belongs.
#[derive(Debug)]
pub enum OpIntent {
    FocusHints(ShownFocus),
}

#[derive(Debug)]
pub enum JobResult {
    WorkspaceOp {
        intent: OpIntent,
        result: Result<(), DaemonError>,
    },
    Resized {
        pane: PaneId,
        result: Result<(), DaemonError>,
    },
}

/// Run `fut` beside the loop and post its outcome under `tag`. Jobs are
/// never aborted: cancelling a request after its write started would leave
/// the daemon's side of it half done. A closed channel means the loop is
/// gone and the outcome has nowhere to land.
pub fn spawn_job(
    tx: &UnboundedSender<JobOutcome>,
    tag: JobTag,
    fut: impl Future<Output = JobResult> + Send + 'static,
) {
    let tx = tx.clone();
    tokio::spawn(async move {
        let result = fut.await;
        let _ = tx.send(JobOutcome { tag, result });
    });
}

/// One value in flight per key and the latest offer held behind it.
#[derive(Debug)]
pub struct Coalescer<K, T> {
    busy: HashSet<K>,
    held: HashMap<K, T>,
}

impl<K, T> Default for Coalescer<K, T> {
    fn default() -> Self {
        Self {
            busy: HashSet::new(),
            held: HashMap::new(),
        }
    }
}

impl<K: Eq + Hash + Clone, T> Coalescer<K, T> {
    /// `Some(value)` when the key was idle and the value goes out now; a
    /// busy key holds it, replacing any older held value.
    pub fn offer(&mut self, key: K, value: T) -> Option<T> {
        if self.busy.insert(key.clone()) {
            return Some(value);
        }
        self.held.insert(key, value);
        None
    }

    /// The key's value came back: release the held value, which is then in
    /// flight, or let the key go idle.
    pub fn settle(&mut self, key: &K) -> Option<T> {
        let next = self.held.remove(key);
        if next.is_none() {
            self.busy.remove(key);
        }
        next
    }

    fn forget(&mut self, key: &K) {
        self.busy.remove(key);
        self.held.remove(key);
    }
}

/// The loop's record of which job each key waits for.
#[derive(Debug)]
pub struct JobLedger<T> {
    next_id: u64,
    in_flight: HashMap<JobKey, (u64, Generation)>,
    coalesced: Coalescer<JobKey, T>,
}

impl<T> Default for JobLedger<T> {
    fn default() -> Self {
        Self {
            next_id: 0,
            in_flight: HashMap::new(),
            coalesced: Coalescer::default(),
        }
    }
}

impl<T> JobLedger<T> {
    /// Offer `value` for `key`; a tag comes back when it goes out now.
    pub fn issue(&mut self, key: JobKey, generation: Generation, value: T) -> Option<(JobTag, T)> {
        let value = self.coalesced.offer(key.clone(), value)?;
        Some((self.tag(key, generation), value))
    }

    /// The job behind `tag` finished. Only the job its key waits for settles
    /// it; a late outcome from a forgotten generation releases nothing. The
    /// held follow-up, if any, is issued on `generation`.
    pub fn settle(&mut self, tag: &JobTag, generation: Generation) -> Option<(JobTag, T)> {
        if self.in_flight.get(&tag.key) != Some(&(tag.id, tag.generation)) {
            return None;
        }
        self.in_flight.remove(&tag.key);
        let value = self.coalesced.settle(&tag.key)?;
        Some((self.tag(tag.key.clone(), generation), value))
    }

    /// A reconnect: outcomes from `generation` will be dropped, so their
    /// keys are free and what they held goes with them.
    pub fn forget_generation(&mut self, generation: Generation) {
        let coalesced = &mut self.coalesced;
        self.in_flight.retain(|key, &mut (_, issued)| {
            if issued == generation {
                coalesced.forget(key);
                return false;
            }
            true
        });
    }

    /// The id of the job `key` waits for.
    pub fn in_flight(&self, key: &JobKey) -> Option<u64> {
        self.in_flight.get(key).map(|&(id, _)| id)
    }

    fn tag(&mut self, key: JobKey, generation: Generation) -> JobTag {
        self.next_id += 1;
        self.in_flight
            .insert(key.clone(), (self.next_id, generation));
        JobTag {
            id: self.next_id,
            generation,
            key,
        }
    }
}

/// What a key's queued wish carries until it is issued.
#[derive(Debug)]
pub(super) enum Pending {
    FocusHints(ShownFocus),
    Geometry(PaneId, u16, u16),
}

/// One entry of the geometry pass: a shown pane, its inner rect and the
/// attachment it was sized on (empty while the pane is not live).
type ShownGeometry = (PaneId, u16, u16, String);

/// The loop's jobs: the outcome channel, the ledger and the memos that
/// decide when a new job is wanted.
#[derive(Debug)]
pub(super) struct LoopJobs {
    tx: UnboundedSender<JobOutcome>,
    pub(super) rx: UnboundedReceiver<JobOutcome>,
    pub(super) ledger: JobLedger<Pending>,
    generation: Generation,
    pub(super) focus: FocusMemo,
    sent_geometry: Vec<ShownGeometry>,
}

impl LoopJobs {
    pub(super) fn new(workspace: &Workspace<LiveDaemon>) -> Self {
        let (tx, rx) = mpsc::unbounded_channel();
        Self {
            tx,
            rx,
            ledger: JobLedger::default(),
            generation: workspace.daemon().generation(),
            focus: FocusMemo::new(workspace),
            sent_geometry: Vec::new(),
        }
    }

    /// Follow the daemon connection: a new generation frees every key the
    /// old one held.
    pub(super) fn sync_generation(&mut self, current: Generation) -> Generation {
        if current != self.generation {
            self.ledger.forget_generation(self.generation);
            self.generation = current;
        }
        current
    }

    /// Offer `pending` for `key` and start whatever goes out.
    pub(super) fn offer(
        &mut self,
        workspace: &mut Workspace<LiveDaemon>,
        chrome: &mut Chrome,
        key: JobKey,
        pending: Pending,
    ) {
        let generation = self.sync_generation(workspace.daemon().generation());
        let issued = self.ledger.issue(key, generation, pending);
        self.start(workspace, chrome, issued);
    }

    /// Start each issued job. A wish with nothing left to send settles its
    /// key at once, so a key is never left waiting on a job that does not
    /// exist.
    pub(super) fn start(
        &mut self,
        workspace: &mut Workspace<LiveDaemon>,
        chrome: &mut Chrome,
        mut issued: Option<(JobTag, Pending)>,
    ) {
        while let Some((tag, pending)) = issued {
            if self.dispatch(workspace, chrome, &tag, pending) {
                return;
            }
            issued = self.ledger.settle(&tag, self.generation);
        }
    }

    fn dispatch(
        &mut self,
        workspace: &mut Workspace<LiveDaemon>,
        chrome: &mut Chrome,
        tag: &JobTag,
        pending: Pending,
    ) -> bool {
        match pending {
            Pending::FocusHints(focus) => {
                let Some(op) = focus_hints::focus_hints_op(workspace, &focus) else {
                    self.focus.refused();
                    return false;
                };
                issue_workspace_op(
                    workspace.daemon(),
                    &self.tx,
                    tag.clone(),
                    op,
                    OpIntent::FocusHints(focus),
                );
                true
            }
            Pending::Geometry(pane, rows, cols) => {
                match workspace.stage_geometry(pane, rows, cols) {
                    Ok(messages) if messages.is_empty() => false,
                    Ok(messages) => {
                        let daemon = workspace.daemon().clone();
                        spawn_job(&self.tx, tag.clone(), async move {
                            let mut result = Ok(());
                            for message in messages {
                                result = daemon.notify(message).await;
                                if result.is_err() {
                                    break;
                                }
                            }
                            JobResult::Resized { pane, result }
                        });
                        true
                    }
                    // The pane says so; a later pass sizes it again.
                    Err(FrameError::Backpressure) => {
                        self.sent_geometry.retain(|entry| entry.0 != pane);
                        false
                    }
                    Err(error) => {
                        chrome.notify(Toast::error(error.to_string()));
                        false
                    }
                }
            }
        }
    }
}

/// Send `op` beside the loop; its outcome comes back tagged with `intent`.
pub(super) fn issue_workspace_op(
    daemon: &LiveDaemon,
    tx: &UnboundedSender<JobOutcome>,
    tag: JobTag,
    op: WorkspaceOp,
    intent: OpIntent,
) {
    let daemon = daemon.clone();
    spawn_job(tx, tag, async move {
        let result = daemon.workspace_op(op).await.map(|_| ());
        JobResult::WorkspaceOp { intent, result }
    });
}

/// Give every shown live pane the geometry it does not hold yet. The memo
/// is what earlier passes asked for, so an unchanged pane costs nothing
/// while a new rect, a new attachment or a pane that just went live is
/// sized. The rects come from `chrome.view` as the last draw left it:
/// recomputing the view here would drop the hit areas that draw recorded.
pub(super) fn stage_live_geometry<B: Backend>(
    terminal: &mut Terminal<B>,
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
    jobs: &mut LoopJobs,
) {
    let area = match terminal.size() {
        Ok(area) => area,
        Err(error) => {
            chrome.notify(Toast::error(error.to_string()));
            return;
        }
    };
    if area.width == 0 || area.height == 0 {
        return;
    }
    let shown: Vec<ShownGeometry> = match chrome.active_tab() {
        Some(tab) => chrome
            .view
            .pane_infos
            .iter()
            .filter_map(|info| {
                let pane_id = *tab.slots.get(&info.id)?;
                let pane = workspace.panes.get(&pane_id)?;
                let attachment = if pane.is_live() {
                    pane.attachment_id().to_string()
                } else {
                    String::new()
                };
                Some((
                    pane_id,
                    info.inner_rect.height,
                    info.inner_rect.width,
                    attachment,
                ))
            })
            .collect(),
        None => Vec::new(),
    };
    let updates: Vec<(PaneId, u16, u16)> = shown
        .iter()
        .filter(|entry| !entry.3.is_empty() && !jobs.sent_geometry.contains(entry))
        .map(|&(pane_id, rows, cols, _)| (pane_id, rows, cols))
        .collect();
    jobs.sent_geometry = shown;
    for (pane_id, rows, cols) in updates {
        jobs.offer(
            workspace,
            chrome,
            JobKey::Geometry(pane_id),
            Pending::Geometry(pane_id, rows, cols),
        );
    }
}
