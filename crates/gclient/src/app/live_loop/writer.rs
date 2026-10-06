//! The per-pane writer (plan A2 of gclient-daemon-resilience). Each pane that
//! types through the daemon gets one task that sends its writes in the order
//! they were typed and posts one outcome per write back to the loop, so a held
//! `terminal_input` reply never sits between two frames. `client_write_seq`
//! is still assigned on the loop; the writer only carries the body.

use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Arc;

use serde_json::Value;
use tokio::sync::mpsc::{self, UnboundedSender};
use tokio::sync::watch;

use crate::copy_mode::PASTE_MAX_BYTES;
use crate::daemon::{Daemon, Generation, LiveDaemon};
use crate::frame_source::{FrameError, WriteReceipt};

use super::super::PaneId;
use super::jobs::{JobKey, JobOutcome, JobResult, JobTag};

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
            while let Some(queued) = rx.recv().await {
                match queued {
                    QueuedWrite::Write {
                        generation,
                        client_write_seq,
                        body,
                        bytes,
                    } => {
                        drained.messages.fetch_sub(1, Ordering::AcqRel);
                        drained.bytes.fetch_sub(bytes, Ordering::AcqRel);
                        // The body names the attachment the write was typed
                        // for; its outcome speaks only for that attachment.
                        let attachment = body["attachment_id"]
                            .as_str()
                            .unwrap_or_default()
                            .to_owned();
                        // An unconfirmed write leaves the pane read-only, so
                        // nothing queued behind it may go out under that
                        // uncertainty; nor may a write typed for a connection
                        // that has since been replaced.
                        if drained.stopped.load(Ordering::Acquire)
                            || generation != daemon.generation()
                        {
                            abandoned.add(generation, client_write_seq, attachment, bytes);
                            if rx.is_empty() && !abandoned.report(&outcomes, pane) {
                                return;
                            }
                            continue;
                        }
                        let result = match daemon.send(body).await {
                            Ok(reply) => JobResult::Write { pane, bytes, reply },
                            Err(_) => {
                                drained.stopped.store(true, Ordering::Release);
                                JobResult::WriteUnconfirmed { pane, attachment }
                            }
                        };
                        let outcome = JobOutcome {
                            tag: JobTag {
                                id: client_write_seq,
                                generation,
                                key: JobKey::Write(pane),
                            },
                            result,
                        };
                        if outcomes.send(outcome).is_err() {
                            return;
                        }
                    }
                    QueuedWrite::Barrier {
                        message,
                        after,
                        flushed,
                        attachment,
                        done,
                    } => {
                        if !abandoned.report(&outcomes, pane) {
                            return;
                        }
                        if let Some(mut taken) = after {
                            let _ = taken.wait_for(|taken| *taken).await;
                        }
                        // A direct pane's input is flushed to its frame socket
                        // first. The frame writer bounds each write and retires
                        // a socket the host stopped reading, which fails the
                        // receipt: what was typed there is unconfirmed, and the
                        // barrier goes on rather than holding the next take.
                        if let Some(flushed) = flushed {
                            if !matches!(flushed.await, Ok(Ok(()))) {
                                let outcome = JobOutcome {
                                    tag: JobTag {
                                        id: 0,
                                        generation: writer_generation,
                                        key: JobKey::Write(pane),
                                    },
                                    result: JobResult::WriteUnconfirmed { pane, attachment },
                                };
                                if outcomes.send(outcome).is_err() {
                                    return;
                                }
                            }
                        }
                        // A release that cannot reach the daemon lost the
                        // connection, which drops the lease with it.
                        if let Some(message) = message {
                            let _ = daemon.notify(message).await;
                        }
                        let _ = done.send(true);
                    }
                }
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
