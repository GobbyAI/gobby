//! Daemon work the live loop runs beside itself (plan A1 of
//! gclient-daemon-resilience). A job is a spawned future whose outcome comes
//! back on one channel the loop selects on, so no daemon round trip ever sits
//! between two frames. Each key keeps one job in flight and the latest
//! wish pending; the ledger tags every job with the connection generation
//! it was issued on, and only the job a key still waits for settles it.

use std::collections::{HashMap, HashSet};
use std::future::Future;
use std::hash::Hash;
use std::time::{Duration, Instant};

use gobby_terminal::protocol::ClientMessage;
use gobby_terminal::terminal_theme::ThemeDeclaration;
use ratatui::backend::Backend;
use ratatui::Terminal;
use serde_json::{json, Value};
use tokio::sync::mpsc::{self, UnboundedReceiver, UnboundedSender};

use crate::daemon::{
    Daemon, DaemonError, Generation, LiveDaemon, SpawnOutcome, TerminalRow, WorkspaceOp,
    WorkspaceSnapshot,
};
use crate::frame_source::{FrameError, FrameSource, PaneFrameSource};
use crate::ui::status::Toast;
use crate::ui::Chrome;

use super::super::attention::{PendingAttention, ResponseSubmission};
use super::super::live::relist::Relist;
use super::super::live::RosterSnapshot;
use super::super::{PaneId, Workspace};
use super::chain_apply;
use super::focus_hints::{self, FocusMemo, ShownFocus};
use super::mouse::Placement;

#[cfg(test)]
mod tests;

/// What a job is about. One job per key is in flight at a time.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum JobKey {
    Geometry(PaneId),
    /// A pane's scroll offset; the latest one wins.
    Scroll(PaneId),
    /// A proxied pane's terminal theme relay; the latest theme wins.
    ThemeRelay(PaneId),
    /// The focus report; the latest one wins, and a report in flight holds the next.
    FocusReport,
    FocusHints,
    /// A pane writer's outcomes; the tag id is the write's `client_write_seq`.
    Write(PaneId),
    Respond,
    /// A layout op; ops are not coalesced, so every outcome applies.
    Op,
    /// The read that opens a placed terminal no pane shows; one per terminal.
    Terminal(String),
    /// A project's roster read; one per project, so a read for one project
    /// never displaces another's.
    Roster(String),
    /// The attention roster read; the latest one wins.
    Attention,
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
    /// A proxied pane's scroll offset reached the daemon, or failed to.
    Scrolled { result: Result<(), DaemonError> },
    /// The daemon's answer to a proxied pane's theme relay. A failure leaves
    /// the pane's theme undeclared, so the next tick relays it again.
    ThemeRelayed {
        pane: PaneId,
        theme: ThemeDeclaration,
        result: Result<(), DaemonError>,
    },
    /// The daemon's answer to the focus report; a failure hands it back to the
    /// focus hold to send again.
    FocusReported { result: Result<(), DaemonError> },
    /// The daemon's reply to one write of `bytes`.
    Write {
        pane: PaneId,
        bytes: usize,
        reply: Value,
    },
    /// A write whose send failed, so the daemon may or may not have it.
    /// `attachment` is the one it was typed for.
    WriteUnconfirmed { pane: PaneId, attachment: String },
    /// Writes the writer dropped unsent after an unconfirmed write or a
    /// connection change, with the attachment the newest was typed for.
    WriteAbandoned {
        pane: PaneId,
        attachment: String,
        messages: usize,
        bytes: usize,
    },
    /// Kills of some panes' terminals, then their close op. `kept` names the
    /// panes whose kill was refused or never sent.
    Killed {
        killed: bool,
        kept: Vec<PaneId>,
        result: Result<(), DaemonError>,
    },
    Responded {
        pending: PendingAttention,
        result: Result<(), DaemonError>,
    },
    /// A layout op or op chain; `unplaced` names a terminal whose expected
    /// placement it never made.
    Ops {
        unplaced: Option<String>,
        result: Result<(), DaemonError>,
    },
    /// An arrange's op chain. `focus` is the pane focused when it was issued,
    /// refocused once the chain's answer lands, whether or not an op refused.
    Arranged {
        focus: Option<PaneId>,
        result: Result<(), DaemonError>,
    },
    /// A new grid's chain. `tab` is the tab it created, focused once the
    /// chain's answer lands, whether or not a later step refused.
    Grid {
        project: String,
        tab: Option<String>,
        result: Result<(), DaemonError>,
    },
    /// The read that opens a placed terminal no pane shows. `elapsed` spans
    /// the read, for the WARN when it fails.
    Opened {
        terminal_id: String,
        elapsed: Duration,
        result: Result<TerminalRow, DaemonError>,
    },
    /// A project's roster read. `apply_relist` installs it only while that
    /// project is focused, so a read that lands after a switch is dropped.
    /// `elapsed` spans the read, for the WARN when it fails.
    Roster {
        project: String,
        elapsed: Duration,
        result: Result<Relist, DaemonError>,
    },
    /// The attention roster read. An attention event newer than it wins.
    /// `elapsed` spans the read, for the WARN when it fails.
    Attention {
        elapsed: Duration,
        result: Result<RosterSnapshot, DaemonError>,
    },
    /// A legacy spawn's `terminal_create` reply. `project` is the project it
    /// was asked of, so a reply that lands after a switch is an orphan.
    Spawned {
        then: PaneAction,
        project: Option<String>,
        result: Result<SpawnOutcome, DaemonError>,
        roster: Option<Result<Relist, DaemonError>>,
    },
    /// An owned shell's create chain: its create, the draft it replaced and
    /// its tab order, as one action's reply.
    Shell {
        project: Option<String>,
        result: Result<OwnedShell, DaemonError>,
    },
    /// A local tab adoption chain. `new_tab` is the tab the chain committed,
    /// present even when a later step failed and the chain then rolled back.
    Adopted {
        project: Option<String>,
        old_tab: String,
        adopted: AdoptedShell,
    },
    /// A disposal of a committed orphan, keyed by what it names.
    Disposed {
        orphan: Orphan,
        outcome: DisposalOutcome,
    },
}

/// What an owned shell's create chain committed.
#[derive(Debug)]
pub struct OwnedShell {
    pub terminal_id: String,
    /// The pane the daemon placed the shell in.
    pub pane_id: String,
    /// The draft tab the new shell replaced, and the tab the daemon made for it.
    pub draft: Option<(String, String)>,
    /// The daemon's workspace after a draft replacement, installed on the loop.
    pub snapshot: Option<WorkspaceSnapshot>,
    /// A tab-order save the daemon refused, named for a toast.
    pub tab_order_warning: Option<String>,
    /// The project roster read after the create, applied while it is current.
    pub roster: Option<Result<Relist, DaemonError>>,
}

/// What an adoption chain committed. `new_tab` is `None` when the daemon
/// refused the create, and `rolled_back` is false when its rollback failed.
#[derive(Debug)]
pub struct AdoptedShell {
    pub new_tab: Option<String>,
    pub rolled_back: bool,
    pub terminal_id: Result<String, DaemonError>,
    pub snapshot: Option<WorkspaceSnapshot>,
    pub tab_order_warning: Option<String>,
    pub roster: Option<Result<Relist, DaemonError>>,
}

/// A committed mutation the loop must dispose of once its target is gone.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Orphan {
    Terminal(String),
    Tab(String),
}

/// How a disposal settled.
#[derive(Debug)]
pub enum DisposalOutcome {
    /// Closed, or already gone.
    Done,
    /// Refused by the daemon, with its reason.
    Refused(String),
    /// Lost or failed before an answer.
    Failed(DaemonError),
}

/// A committed orphan awaiting disposal. A settled disposal waits for the
/// model to move past `settled.watermark` before it is tried again.
#[derive(Debug, Clone)]
pub struct CommittedOrphan {
    pub orphan: Orphan,
    pub in_flight: bool,
    pub settled: Option<Settled>,
}

/// How the last disposal of an orphan settled, against which model.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Settled {
    pub model_install: Option<(Generation, u64)>,
    /// The daemon never answered, so the target may already be gone.
    pub lost: bool,
}

/// A committed terminal whose pane the loop acts on once the roster shows it.
#[derive(Debug)]
pub struct AwaitingPane {
    pub terminal_id: String,
    pub then: PaneAction,
}

/// What lands on a committed terminal's pane.
#[derive(Debug, Clone)]
pub enum PaneAction {
    Place {
        placement: Placement,
        worktree_id: Option<String>,
    },
    Focus,
    /// Only attaches the pane; settling any pane already marks it due.
    Attach,
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
    /// Committed mutations the loop must dispose of; see `live_loop::chain_apply`.
    pub committed_orphans: Vec<CommittedOrphan>,
    /// Committed terminals whose pane has not reached the roster yet.
    pub awaiting_panes: Vec<AwaitingPane>,
}

impl<T> Default for JobLedger<T> {
    fn default() -> Self {
        Self {
            next_id: 0,
            in_flight: HashMap::new(),
            coalesced: Coalescer::default(),
            committed_orphans: Vec::new(),
            awaiting_panes: Vec::new(),
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
    Scroll(PaneId, u32),
    ThemeRelay(PaneId, ThemeDeclaration),
    FocusReport(Value),
    Open(String),
    Roster(String),
    Attention,
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
    /// Set when the focus report's send failed, so the focus hold sends it again.
    focus_refused: bool,
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
            focus_refused: false,
        }
    }

    /// Whether a focus report is in flight; the next report waits for its answer.
    pub(super) fn focus_report_in_flight(&self) -> bool {
        self.ledger.in_flight(&JobKey::FocusReport).is_some()
    }

    /// Whether the focus report's send failed since this was last asked.
    pub(super) fn take_focus_refused(&mut self) -> bool {
        std::mem::take(&mut self.focus_refused)
    }

    pub(super) fn focus_report_refused(&mut self) {
        self.focus_refused = true;
    }

    /// Follow the daemon connection: a new generation frees every key the
    /// old one held.
    /// Where a long-lived task, such as a pane writer, posts its outcomes.
    pub(super) fn outcomes(&self) -> &UnboundedSender<JobOutcome> {
        &self.tx
    }

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

    /// Issues the roster and attention reads live events flagged. Each read
    /// is a coalesced job, so a flag raised while one is in flight waits for it.
    pub(super) fn drain_pending_refetch(
        &mut self,
        workspace: &mut Workspace<LiveDaemon>,
        chrome: &mut Chrome,
    ) {
        chain_apply::settle_panes(self, workspace, chrome);
        chain_apply::evaluate(self, workspace);
        let pending = std::mem::take(&mut workspace.pending_refetch);
        for project in pending.rosters {
            self.offer(
                workspace,
                chrome,
                JobKey::Roster(project.clone()),
                Pending::Roster(project),
            );
        }
        if pending.attention {
            self.offer(workspace, chrome, JobKey::Attention, Pending::Attention);
        }
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
            Pending::ThemeRelay(pane_id, theme) => {
                let Some(message) = workspace
                    .panes
                    .get(&pane_id)
                    .and_then(|pane| pane.theme_relay(&theme))
                else {
                    return false;
                };
                let daemon = workspace.daemon().clone();
                spawn_job(&self.tx, tag.clone(), async move {
                    let result = daemon.notify(message).await;
                    JobResult::ThemeRelayed {
                        pane: pane_id,
                        theme,
                        result,
                    }
                });
                true
            }
            Pending::FocusReport(message) => {
                let daemon = workspace.daemon().clone();
                spawn_job(&self.tx, tag.clone(), async move {
                    let result = daemon.notify(message).await;
                    JobResult::FocusReported { result }
                });
                true
            }
            Pending::Roster(project) => {
                let read = workspace.issue_roster_fetch(project.clone());
                spawn_job(&self.tx, tag.clone(), async move {
                    let started = Instant::now();
                    let result = read.await;
                    JobResult::Roster {
                        project,
                        elapsed: started.elapsed(),
                        result,
                    }
                });
                true
            }
            Pending::Attention => {
                let daemon = workspace.daemon().clone();
                spawn_job(&self.tx, tag.clone(), async move {
                    let started = Instant::now();
                    let result = daemon.attention_roster().await;
                    JobResult::Attention {
                        elapsed: started.elapsed(),
                        result,
                    }
                });
                true
            }
            Pending::Open(terminal_id) => {
                // The row may have landed, or the pane closed or the focus
                // moved, since this follow-up was queued.
                if !workspace.is_unresolved_terminal(&terminal_id) {
                    return false;
                }
                issue_terminal_open(workspace.daemon(), &self.tx, tag.clone(), terminal_id);
                true
            }
            Pending::Scroll(pane_id, rows) => {
                let daemon = workspace.daemon().clone();
                let Some(pane) = workspace.panes.get_mut(&pane_id) else {
                    return false;
                };
                let body = json!({
                    "type": "terminal_set_scroll_offset",
                    "terminal_id": pane.terminal_id,
                    "attachment_id": pane.attachment_id(),
                    "rows_from_live_edge": rows,
                });
                match pane.frame_source_mut() {
                    Some(PaneFrameSource::Proxy(_)) => {
                        spawn_job(&self.tx, tag.clone(), async move {
                            JobResult::Scrolled {
                                result: daemon.notify(body).await,
                            }
                        });
                        true
                    }
                    // A host socket takes it without a round trip; a full
                    // writer is the pane's status, as for typed input.
                    Some(source) => {
                        let message = ClientMessage::SetScrollOffset {
                            rows_from_live_edge: rows,
                        };
                        match source.send_input(&message) {
                            Ok(()) => {}
                            Err(error @ FrameError::Backpressure) => {
                                pane.status_message = Some(error.to_string());
                            }
                            Err(error) => chrome.notify(Toast::error(error.to_string())),
                        }
                        false
                    }
                    None => false,
                }
            }
        }
    }
}

/// Send `op` beside the loop; its outcome comes back tagged with `intent`.
/// Send an attention answer beside the loop; its outcome toasts at apply.
pub(super) fn issue_response(daemon: &LiveDaemon, jobs: &LoopJobs, submission: ResponseSubmission) {
    let tag = JobTag {
        id: 0,
        generation: daemon.generation(),
        key: JobKey::Respond,
    };
    let daemon = daemon.clone();
    spawn_job(&jobs.tx, tag, async move {
        let (pending, result) = submission.send(daemon).await;
        JobResult::Responded { pending, result }
    });
}

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

pub(super) fn issue_terminal_open(
    daemon: &LiveDaemon,
    tx: &UnboundedSender<JobOutcome>,
    tag: JobTag,
    terminal_id: String,
) {
    let daemon = daemon.clone();
    spawn_job(tx, tag, async move {
        let started = Instant::now();
        let result = daemon.terminal(&terminal_id).await;
        JobResult::Opened {
            terminal_id,
            elapsed: started.elapsed(),
            result,
        }
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
