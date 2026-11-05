//! The per-pane writer (plan A2 of gclient-daemon-resilience). Each pane that
//! types through the daemon gets one task that sends its writes in the order
//! they were typed and posts one outcome per write back to the loop, so a held
//! `terminal_input` reply never sits between two frames. `client_write_seq`
//! is still assigned on the loop; the writer only carries the body.

use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Arc;

use serde_json::Value;
use tokio::sync::mpsc::{self, UnboundedSender};
use tokio::sync::watch;
use tokio::task::JoinHandle;

use crate::copy_mode::PASTE_MAX_BYTES;
use crate::daemon::{Daemon, DaemonError, Generation, LiveDaemon};
use crate::frame_source::{FrameError, WriteReceipt};

use super::super::PaneId;
use super::jobs::{JobKey, JobOutcome, JobResult, JobTag};

/// A write sent to the daemon whose reply has not settled yet. Writes settle
/// in the order they were sent.
struct InFlight {
    client_write_seq: u64,
    generation: Generation,
    attachment: String,
    bytes: usize,
    reply: JoinHandle<Result<Value, DaemonError>>,
}

/// The outcome of the oldest in-flight write, and whether it went unconfirmed.
struct Settled {
    outcome: JobOutcome,
    unconfirmed: bool,
    bytes: usize,
}

/// Waits for the oldest in-flight write's reply. A reply that never arrives (a
/// deadline, a lost connection, a failed task) leaves that write unconfirmed.
async fn settle_front(inflight: &mut VecDeque<InFlight>, pane: PaneId) -> Settled {
    let front = inflight
        .front_mut()
        .expect("settled only while a write is in flight");
    let joined = (&mut front.reply).await;
    let front = inflight
        .pop_front()
        .expect("the front write stays queued until it settles");
    let (result, unconfirmed) = match joined {
        Ok(Ok(reply)) => (
            JobResult::Write {
                pane,
                bytes: front.bytes,
                reply,
            },
            false,
        ),
        Ok(Err(_)) | Err(_) => (
            JobResult::WriteUnconfirmed {
                pane,
                attachment: front.attachment,
            },
            true,
        ),
    };
    Settled {
        outcome: JobOutcome {
            tag: JobTag {
                id: front.client_write_seq,
                generation: front.generation,
                key: JobKey::Write(pane),
            },
            result,
        },
        unconfirmed,
        bytes: front.bytes,
    }
}

/// Frees a settled write's backlog slot, stops the writer on an unconfirmed
/// write, and sends its outcome. False once the outcomes channel has closed.
fn deliver(settled: Settled, backlog: &Backlog, outcomes: &UnboundedSender<JobOutcome>) -> bool {
    backlog.messages.fetch_sub(1, Ordering::AcqRel);
    backlog.bytes.fetch_sub(settled.bytes, Ordering::AcqRel);
    if settled.unconfirmed {
        backlog.stopped.store(true, Ordering::Release);
    }
    outcomes.send(settled.outcome).is_ok()
}

/// Writes a pane may have queued behind the one being sent. The cap counts
/// writes only: the channel itself is unbounded so a barrier always fits
/// behind a full queue, however many a pane's focus moves stack up.
const WRITE_QUEUE_MAX_MESSAGES: usize = 256;

#[derive(Debug)]
enum QueuedWrite {
    Write {
        generation: Generation,
        client_write_seq: u64,
        body: Value,
        bytes: usize,
    },
    /// A point every write accepted before it has passed, carrying the daemon
    /// request (if any) that must follow them, when `after` is set the take
    /// it undoes, and when `flushed` is set the pane's last direct frame
    /// write, typed for `attachment`. `done` resolves once it was reached and
    /// sent.
    Barrier {
        message: Option<Value>,
        after: Option<watch::Receiver<bool>>,
        flushed: Option<WriteReceipt>,
        attachment: String,
        done: watch::Sender<bool>,
    },
}

/// A barrier the writer has taken but not yet fired. It fires once the writes
/// sent before it have settled, and its waits then run off the writer task.
struct Fence {
    /// Writes sent before the barrier, counted when it was taken.
    ahead: u64,
    message: Option<Value>,
    after: Option<watch::Receiver<bool>>,
    flushed: Option<WriteReceipt>,
    attachment: String,
    done: watch::Sender<bool>,
}

/// The writer's barriers, fired in the order they were taken.
#[derive(Default)]
struct Fences {
    queue: VecDeque<Fence>,
    /// Writes that have entered `inflight`.
    pushed: u64,
    /// Writes whose replies have settled, in the order sent.
    settled: u64,
    /// The last fired barrier's `done`: the next one waits behind it so detaches
    /// reach the daemon in order.
    chain: Option<watch::Receiver<bool>>,
}

impl Fences {
    /// Fires every barrier whose earlier writes have settled. `false` means the
    /// pane is finished and the writer must stop.
    fn fire_ready(
        &mut self,
        abandoned: &mut Abandoned,
        outcomes: &UnboundedSender<JobOutcome>,
        daemon: &LiveDaemon,
        pane: PaneId,
        generation: Generation,
    ) -> bool {
        while self
            .queue
            .front()
            .is_some_and(|fence| fence.ahead <= self.settled)
        {
            if !abandoned.report(outcomes, pane) {
                return false;
            }
            let Some(fence) = self.queue.pop_front() else {
                break;
            };
            let prev = self.chain.replace(fence.done.subscribe());
            tokio::spawn(run_fence(
                fence,
                prev,
                daemon.clone(),
                outcomes.clone(),
                generation,
                pane,
            ));
        }
        true
    }
}

/// Runs one fired barrier off the writer task: the barrier before it, the take
/// it undoes and the frame flush come first, then the daemon request.
async fn run_fence(
    fence: Fence,
    prev: Option<watch::Receiver<bool>>,
    daemon: LiveDaemon,
    outcomes: UnboundedSender<JobOutcome>,
    generation: Generation,
    pane: PaneId,
) {
    let Fence {
        message,
        after,
        flushed,
        attachment,
        done,
        ..
    } = fence;
    if let Some(mut prev) = prev {
        let _ = prev.wait_for(|fired| *fired).await;
    }
    if let Some(mut taken) = after {
        let _ = taken.wait_for(|taken| *taken).await;
    }
    // A direct pane's input is flushed to its frame socket first. The frame
    // writer bounds each write and retires a socket the host stopped reading,
    // which fails the receipt: what was typed there is unconfirmed, and the
    // barrier goes on rather than holding the next take.
    if let Some(flushed) = flushed {
        if !matches!(flushed.await, Ok(Ok(()))) {
            let outcome = JobOutcome {
                tag: JobTag {
                    id: 0,
                    generation,
                    key: JobKey::Write(pane),
                },
                result: JobResult::WriteUnconfirmed { pane, attachment },
            };
            if outcomes.send(outcome).is_err() {
                return;
            }
        }
    }
    // A detach that cannot reach the daemon lost the connection, which drops
    // the attachment with it.
    if let Some(message) = message {
        let _ = daemon.notify(message).await;
    }
    let _ = done.send(true);
}

/// What is queued and not yet picked up by the writer task.
#[derive(Debug, Default)]
struct Backlog {
    messages: AtomicUsize,
    bytes: AtomicUsize,
    /// Set once a write went unconfirmed; later writes are not sent.
    stopped: AtomicBool,
}

/// Writes skipped since the last report, tagged with the newest of them and
/// the attachment it was typed for.
#[derive(Debug, Default)]
struct Abandoned {
    tag: Option<(Generation, u64, String)>,
    messages: usize,
    bytes: usize,
}

impl Abandoned {
    fn add(
        &mut self,
        generation: Generation,
        client_write_seq: u64,
        attachment: String,
        bytes: usize,
    ) {
        self.tag = Some((generation, client_write_seq, attachment));
        self.messages += 1;
        self.bytes += bytes;
    }

    /// Post one report for everything skipped so far; `false` once the loop
    /// is gone.
    fn report(&mut self, outcomes: &UnboundedSender<JobOutcome>, pane: PaneId) -> bool {
        let Some((generation, id, attachment)) = self.tag.take() else {
            return true;
        };
        let outcome = JobOutcome {
            tag: JobTag {
                id,
                generation,
                key: JobKey::Write(pane),
            },
            result: JobResult::WriteAbandoned {
                pane,
                attachment,
                messages: std::mem::take(&mut self.messages),
                bytes: std::mem::take(&mut self.bytes),
            },
        };
        outcomes.send(outcome).is_ok()
    }
}

/// The loop's handle on a pane's writer task. Dropping it closes the queue;
/// the task finishes the write it is on and exits.
#[derive(Debug)]
pub(in crate::app) struct PaneWriter {
    tx: UnboundedSender<QueuedWrite>,
    generation: Generation,
    backlog: Arc<Backlog>,
}

impl PaneWriter {
    pub(super) fn spawn(
        daemon: &LiveDaemon,
        outcomes: &UnboundedSender<JobOutcome>,
        pane: PaneId,
    ) -> Self {
        let (tx, mut rx) = mpsc::unbounded_channel();
        let generation = daemon.generation();
        let writer_generation = generation;
        let daemon = daemon.clone();
        let outcomes = outcomes.clone();
        let backlog = Arc::new(Backlog::default());
        let drained = Arc::clone(&backlog);
        tokio::spawn(async move {
            let mut abandoned = Abandoned::default();
            let mut inflight = VecDeque::new();
            let mut fences = Fences::default();
            loop {
                // A settled reply goes out before the next queued write, so the
                // outcomes come back in the order the writes were sent.
                let queued = tokio::select! {
                    biased;
                    settled = settle_front(&mut inflight, pane), if !inflight.is_empty() => {
                        if !deliver(settled, &drained, &outcomes) {
                            return;
                        }
                        fences.settled += 1;
                        if !fences.fire_ready(&mut abandoned, &outcomes, &daemon, pane, writer_generation) {
                            return;
                        }
                        continue;
                    }
                    queued = rx.recv() => queued,
                };
                let Some(queued) = queued else {
                    break;
                };
                match queued {
                    QueuedWrite::Write {
                        generation,
                        client_write_seq,
                        body,
                        bytes,
                    } => {
                        // The body names the attachment the write was typed
                        // for; its outcome speaks only for that attachment.
                        let attachment = body["attachment_id"]
                            .as_str()
                            .unwrap_or_default()
                            .to_owned();
                        // An unconfirmed write stops this writer, so nothing
                        // queued behind it may go out under that uncertainty;
                        // nor may a write typed for a connection
                        // that has since been replaced.
                        if drained.stopped.load(Ordering::Acquire)
                            || generation != daemon.generation()
                        {
                            // An abandoned write never settles, so its slot frees here.
                            drained.messages.fetch_sub(1, Ordering::AcqRel);
                            drained.bytes.fetch_sub(bytes, Ordering::AcqRel);
                            abandoned.add(generation, client_write_seq, attachment, bytes);
                            if rx.is_empty() && !abandoned.report(&outcomes, pane) {
                                return;
                            }
                            continue;
                        }
                        // The next write waits only for this one's frame to reach
                        // the socket, never for the daemon's reply.
                        let (written_tx, mut written_rx) = watch::channel(false);
                        let reply = tokio::spawn({
                            let daemon = daemon.clone();
                            async move { daemon.send_marking_written(body, written_tx).await }
                        });
                        if written_rx.wait_for(|written| *written).await.is_err() {
                            // The frame never reached the socket, so nothing
                            // queued behind it may go out under that.
                            drained.stopped.store(true, Ordering::Release);
                        }
                        inflight.push_back(InFlight {
                            client_write_seq,
                            generation,
                            attachment,
                            bytes,
                            reply,
                        });
                        fences.pushed += 1;
                    }
                    QueuedWrite::Barrier {
                        message,
                        after,
                        flushed,
                        attachment,
                        done,
                    } => {
                        // A detach fences the attachment's waiters: it fires once
                        // every write sent before it has settled. Writes typed
                        // after it keep flowing, so a held reply holds no key.
                        fences.queue.push_back(Fence {
                            ahead: fences.pushed,
                            message,
                            after,
                            flushed,
                            attachment,
                            done,
                        });
                        if !fences.fire_ready(
                            &mut abandoned,
                            &outcomes,
                            &daemon,
                            pane,
                            writer_generation,
                        ) {
                            return;
                        }
                    }
                }
            }
            while !inflight.is_empty() {
                let settled = settle_front(&mut inflight, pane).await;
                if !deliver(settled, &drained, &outcomes) {
                    return;
                }
                fences.settled += 1;
            }
            // Barriers left behind writes that have all settled now fire, so a
            // close the loop asked for still reaches the daemon.
            if !fences.fire_ready(&mut abandoned, &outcomes, &daemon, pane, writer_generation) {
                return;
            }
            abandoned.report(&outcomes, pane);
        });
        Self {
            tx,
            generation,
            backlog,
        }
    }

    /// Stop sending for an attachment the pane replaced: whatever is still
    /// queued is reported unsent, never sent under the old attachment.
    pub(in crate::app) fn retire(&self) {
        self.backlog.stopped.store(true, Ordering::Release);
    }

    /// Whether this writer still sends for the connection `generation`.
    pub(super) fn serves(&self, generation: Generation) -> bool {
        self.generation == generation
            && !self.tx.is_closed()
            && !self.backlog.stopped.load(Ordering::Acquire)
    }

    /// Queue `message` behind every write accepted so far, and behind
    /// `after` and `flushed` when set; `flushed` holds bytes typed for
    /// `attachment`. The returned receiver resolves once it was sent. `None`
    /// means the writer task is gone, which only happens once the loop that
    /// reads its outcomes has exited; the caller must not read that as sent.
    pub(super) fn enqueue_barrier(
        &self,
        message: Option<Value>,
        after: Option<watch::Receiver<bool>>,
        flushed: Option<WriteReceipt>,
        attachment: String,
    ) -> Option<watch::Receiver<bool>> {
        let (done, sent) = watch::channel(false);
        self.tx
            .send(QueuedWrite::Barrier {
                message,
                after,
                flushed,
                attachment,
                done,
            })
            .ok()
            .map(|()| sent)
    }

    /// Queue a write without waiting. A write that would take the backlog
    /// past 256 messages or 1 MiB is refused with `Backpressure`; what is
    /// already queued is never dropped or reordered. `Ok(false)` means the
    /// writer stopped after an unconfirmed write.
    pub(super) fn enqueue(
        &self,
        generation: Generation,
        client_write_seq: u64,
        body: Value,
        bytes: usize,
    ) -> Result<bool, FrameError> {
        let write = QueuedWrite::Write {
            generation,
            client_write_seq,
            body,
            bytes,
        };
        let backlog = &self.backlog;
        if backlog.messages.load(Ordering::Acquire) >= WRITE_QUEUE_MAX_MESSAGES
            || backlog.bytes.load(Ordering::Acquire) + bytes > PASTE_MAX_BYTES
        {
            return Err(FrameError::Backpressure);
        }
        // Only the loop adds, so the check above cannot be overtaken.
        backlog.messages.fetch_add(1, Ordering::AcqRel);
        backlog.bytes.fetch_add(bytes, Ordering::AcqRel);
        if self.tx.send(write).is_ok() {
            return Ok(true);
        }
        backlog.messages.fetch_sub(1, Ordering::AcqRel);
        backlog.bytes.fetch_sub(bytes, Ordering::AcqRel);
        Ok(false)
    }
}
