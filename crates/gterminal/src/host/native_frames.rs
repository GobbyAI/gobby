//! Build and encode native frames without owning the host-wide mutex.

use super::backpressure::{encoded_message_bytes, PushResult};
use super::helpers::truncate_title;
use super::state::{Attachment, HostState};
use crate::pane::PaneTerminal;
use crate::protocol::render_ansi::BlitEncoder;
use crate::protocol::{FrameData, RenderEncoding, ServerMessage, TerminalFrame};
use std::sync::Arc;
use std::time::Instant;

struct Work {
    id: u64,
    host_id: String,
    terminal: Arc<PaneTerminal>,
    rows: u16,
    cols: u16,
    scroll: u32,
    view_revision: u64,
    encoding: RenderEncoding,
    seq: u64,
    title: String,
    desynced: bool,
    encoder: BlitEncoder,
    previous: Option<FrameData>,
}

struct Packet {
    message: ServerMessage,
    bytes: usize,
}

impl Packet {
    fn new(message: ServerMessage) -> Self {
        let bytes = encoded_message_bytes(&message);
        Self { message, bytes }
    }
    fn push(self, attachment: &Attachment, cap: usize) -> PushResult {
        attachment
            .mailbox
            .try_push_prepared(self.message, self.bytes, cap)
    }
    fn replace(self, attachment: &Attachment, cap: usize) -> bool {
        attachment
            .mailbox
            .replace_prepared(self.message, self.bytes, cap)
    }
}

enum Prepared {
    Semantic {
        packet: Packet,
        replacement: Packet,
        frame: FrameData,
    },
    Ansi {
        delta: Packet,
        keyframe: Packet,
        delta_encoder: BlitEncoder,
        key_encoder: BlitEncoder,
    },
}

fn ansi_packet(encoder: &mut BlitEncoder, frame: &FrameData, seq: u64, full: bool) -> Packet {
    let mut encoded = encoder.encode(frame, full);
    let message = ServerMessage::Terminal(TerminalFrame {
        seq,
        width: frame.width,
        height: frame.height,
        full: encoded.full,
        bytes: std::mem::take(&mut encoded.bytes),
    });
    encoder.commit(frame.clone(), encoded);
    Packet::new(message)
}

impl Work {
    fn build(self) -> (Self, u64, String, Option<Prepared>) {
        let (generation, frame) = self
            .terminal
            .frame_for_view(self.cols, self.rows, self.scroll);
        let title = truncate_title(&self.terminal.osc_title());
        let seq = self.seq + u64::from(self.title != title);
        let prepared = match self.encoding {
            RenderEncoding::SemanticFrame
                if !self.desynced && self.previous.as_ref() == Some(&frame) =>
            {
                None
            }
            RenderEncoding::SemanticFrame => Some(Prepared::Semantic {
                packet: Packet::new(ServerMessage::Frame(frame.clone())),
                replacement: Packet::new(ServerMessage::Frame(frame.clone())),
                frame,
            }),
            RenderEncoding::TerminalAnsi if !self.desynced && self.encoder.is_current(&frame) => {
                None
            }
            RenderEncoding::TerminalAnsi => {
                let mut delta_encoder = self.encoder.clone();
                let mut key_encoder = self.encoder.clone();
                let delta = ansi_packet(&mut delta_encoder, &frame, seq, self.desynced);
                let keyframe = ansi_packet(&mut key_encoder, &frame, seq, true);
                Some(Prepared::Ansi {
                    delta,
                    keyframe,
                    delta_encoder,
                    key_encoder,
                })
            }
        };
        (self, generation, title, prepared)
    }
}

fn publish(att: &mut Attachment, prepared: Prepared, desynced: bool, cap: usize) {
    match prepared {
        Prepared::Semantic {
            packet,
            replacement,
            frame,
        } => {
            let result = if desynced && att.mailbox.queued_bytes() > 0 {
                replacement.replace(att, cap);
                PushResult::Overflow
            } else if desynced && att.mailbox.is_writing() {
                PushResult::Overflow
            } else {
                match packet.push(att, cap) {
                    PushResult::Overflow => {
                        replacement.replace(att, cap);
                        PushResult::Overflow
                    }
                    result => result,
                }
            };
            att.desynced = result == PushResult::Overflow;
            if result == PushResult::Queued {
                att.last_semantic_frame = Some(frame);
            }
            if result != PushResult::Closed {
                att.delta_len = att.delta_len.saturating_add(1);
            }
        }
        Prepared::Ansi {
            delta,
            keyframe,
            delta_encoder,
            key_encoder,
        } => {
            if desynced && att.mailbox.is_writing() && att.mailbox.queued_bytes() == 0 {
                att.desynced = true;
            } else if desynced && att.mailbox.queued_bytes() > 0 {
                att.desynced = !keyframe.replace(att, cap);
                if !att.desynced {
                    att.encoder = key_encoder;
                }
            } else {
                match delta.push(att, cap) {
                    PushResult::Queued => {
                        att.encoder = delta_encoder;
                        att.desynced = false;
                    }
                    PushResult::Overflow => {
                        att.desynced = !keyframe.replace(att, cap);
                        if !att.desynced {
                            att.encoder = key_encoder;
                        }
                    }
                    PushResult::Closed => att.desynced = true,
                }
            }
            att.delta_len = att.delta_len.saturating_add(1);
        }
    }
    att.delta_bytes = att.mailbox.queued_bytes();
    att.last_send = Instant::now();
}

impl HostState {
    pub async fn broadcast_frames(self: &Arc<Self>) {
        let mut building = self.frame_build.lock().await;
        let lag = self.lag_timeout();
        {
            let mut inner = self.inner.lock().await;
            let ids: Vec<_> = inner.attachments.keys().copied().collect();
            for id in ids {
                if building.contains(&id) {
                    continue;
                }
                let Some(att) = inner.attachments.get(&id) else {
                    continue;
                };
                tracing::trace!(
                    attachment_id = att.id,
                    reservation_id = att.reservation_id.as_deref(),
                    "broadcast native attachment"
                );
                let host_id = att.host_terminal_id.clone();
                let Some(slot) = inner
                    .by_host_id
                    .get(&host_id)
                    .and_then(|identity| inner.terminals.get(identity))
                else {
                    continue;
                };
                if slot.locator.is_some() {
                    continue;
                }
                let Some(child) = slot.child.as_ref() else {
                    continue;
                };
                let terminal = child.runtime.terminal_handle();
                if !att.desynced
                    && !att.mailbox.is_lagged(lag)
                    && att.built_generation == Some(terminal.content_generation())
                {
                    continue;
                }
                let (seq, title) = (slot.last_seq, slot.title.clone());
                let Ok(gate) = self.mutation_gate.clone().try_read_owned() else {
                    continue;
                };
                let att = inner
                    .attachments
                    .get_mut(&id)
                    .expect("attachment held by host lock");
                let work = Work {
                    id,
                    host_id,
                    terminal,
                    rows: att.rows,
                    cols: att.cols,
                    scroll: att.scroll,
                    view_revision: att.view_revision,
                    encoding: att.encoding,
                    seq,
                    title,
                    desynced: att.desynced,
                    encoder: std::mem::take(&mut att.encoder),
                    previous: att.last_semantic_frame.take(),
                };
                att.desynced = true;
                building.insert(id);
                let state = self.clone();
                tokio::spawn(async move {
                    let _gate = gate;
                    let result = tokio::task::spawn_blocking(move || work.build()).await;
                    let mut building = state.frame_build.lock().await;
                    building.remove(&id);
                    if let Ok((work, generation, title, prepared)) = result {
                        state.publish_frame(work, generation, title, prepared).await;
                    }
                });
            }
        }
    }

    async fn publish_frame(
        &self,
        work: Work,
        generation: u64,
        title: String,
        prepared: Option<Prepared>,
    ) {
        let cap = self.config.delta_queue_bytes as usize;
        let lag = self.lag_timeout();
        let mut inner = self.inner.lock().await;
        let valid_slot = inner
            .by_host_id
            .get(&work.host_id)
            .and_then(|identity| inner.terminals.get(identity))
            .is_some_and(|slot| {
                (slot.last_seq == work.seq
                    || (work.title != title
                        && slot.title == title
                        && slot.last_seq == work.seq.wrapping_add(1)))
                    && slot.child.as_ref().is_some_and(|child| {
                        Arc::ptr_eq(&child.runtime.terminal_handle(), &work.terminal)
                    })
            });
        let valid = valid_slot
            && work.terminal.content_generation() == generation
            && inner.attachments.get(&work.id).is_some_and(|att| {
                att.host_terminal_id == work.host_id
                    && att.rows == work.rows
                    && att.cols == work.cols
                    && att.scroll == work.scroll
                    && att.view_revision == work.view_revision
                    && att.encoding == work.encoding
            });
        let Some(att) = inner.attachments.get_mut(&work.id) else {
            return;
        };
        att.encoder = work.encoder;
        att.last_semantic_frame = work.previous;
        if !valid {
            att.desynced = true;
            return;
        }
        if att.mailbox.is_lagged(lag) {
            att.mailbox.close_with(
                ServerMessage::Error {
                    code: "lagged".into(),
                    message: None,
                },
                cap,
            );
            drop(inner);
            self.detach(work.id).await;
            return;
        }
        att.desynced = work.desynced;
        if let Some(prepared) = prepared {
            publish(att, prepared, work.desynced, cap);
        }
        att.built_generation = Some(generation);
        if let Some(identity) = inner.by_host_id.get(&work.host_id).cloned() {
            if let Some(slot) = inner.terminals.get_mut(&identity) {
                if slot.title != title {
                    slot.title = title;
                    slot.last_seq += 1;
                }
            }
        }
    }
}
