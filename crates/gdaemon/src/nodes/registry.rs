//! Lifetime machine slots fence channel publication and bookkeeping.
use crate::node_commands::{CommandFrame, ReplyFrame};
use std::{
    collections::HashMap,
    future::Future,
    sync::{Arc, Mutex},
};
use tokio::sync::{Mutex as AsyncMutex, mpsc, oneshot};
use uuid::Uuid;

pub struct OutboundCommand {
    pub frame: CommandFrame,
    pub reply: oneshot::Sender<ReplyFrame>,
}

// None inside the map reserves a queued request. The outer None fences a closed
// connection, including senders cloned before a replacement was published.
type ReplySender = Option<oneshot::Sender<ReplyFrame>>;
type Pending = Arc<Mutex<Option<HashMap<Uuid, ReplySender>>>>;

#[derive(Clone)]
pub struct CommandSender {
    sender: mpsc::Sender<OutboundCommand>,
    pending: Pending,
}

pub struct CommandReceiver {
    receiver: mpsc::Receiver<OutboundCommand>,
    pending: Pending,
}

struct PendingGuard {
    id: Uuid,
    pending: Pending,
}

impl Drop for PendingGuard {
    fn drop(&mut self) {
        if let Some(entries) = self
            .pending
            .lock()
            .expect("pending mutex poisoned")
            .as_mut()
        {
            entries.remove(&self.id);
        }
    }
}

impl CommandSender {
    pub async fn request(&self, id: Uuid, frame: CommandFrame) -> Option<ReplyFrame> {
        self.pending
            .lock()
            .expect("pending mutex poisoned")
            .as_mut()?
            .insert(id, None);
        let _guard = PendingGuard {
            id,
            pending: self.pending.clone(),
        };
        let (reply, receiver) = oneshot::channel();
        self.sender
            .send(OutboundCommand { frame, reply })
            .await
            .ok()?;
        receiver.await.ok()
    }

    fn close(&self) {
        // Dropping the senders wakes every HTTP waiter as channel_closed.
        self.pending.lock().expect("pending mutex poisoned").take();
    }
}

impl CommandReceiver {
    pub async fn recv(&mut self) -> Option<CommandFrame> {
        loop {
            let command = self.receiver.recv().await?;
            let id = match &command.frame {
                CommandFrame::Spawn(frame) => &frame.request_id,
                CommandFrame::Cancel(frame) => &frame.request_id,
                CommandFrame::Write(frame) => &frame.request_id,
                CommandFrame::Merge(frame) => &frame.request_id,
                CommandFrame::Remove(frame) => &frame.request_id,
                CommandFrame::Status(frame) => &frame.request_id,
                CommandFrame::Read(frame) => &frame.request_id,
            };
            let Ok(id) = id.parse::<Uuid>() else {
                continue;
            };
            let mut pending = self.pending.lock().expect("pending mutex poisoned");
            let Some(entry) = pending.as_mut().and_then(|entries| entries.get_mut(&id)) else {
                continue;
            };
            if command.reply.is_closed() {
                continue;
            }
            *entry = Some(command.reply);
            return Some(command.frame);
        }
    }

    pub fn resolve(&self, reply: ReplyFrame) {
        let id = match &reply {
            ReplyFrame::Ok(frame) => &frame.request_id,
            ReplyFrame::Err(frame) => &frame.request_id,
        };
        let sender = id.parse::<Uuid>().ok().and_then(|id| {
            self.pending
                .lock()
                .expect("pending mutex poisoned")
                .as_mut()?
                .remove(&id)
                .flatten()
        });
        match sender {
            Some(sender) => {
                let _ = sender.send(reply);
            }
            None => eprintln!("dropping node reply without a pending request: {id}"),
        }
    }
}

impl Drop for CommandReceiver {
    fn drop(&mut self) {
        self.pending.lock().expect("pending mutex poisoned").take();
    }
}

#[derive(Default)]
pub struct Registry {
    slots: Mutex<HashMap<Uuid, Slot>>,
}

#[derive(Default)]
struct Slot {
    current: Option<ChannelHandle>,
    gate: Arc<AsyncMutex<()>>,
}

struct ChannelHandle {
    connection_id: Uuid,
    close: oneshot::Sender<(u16, &'static str)>,
    live: bool,
    commands: CommandSender,
}

#[derive(Debug)]
pub enum PersistOutcome<T> {
    Written(T),
    Skipped,
}

impl Registry {
    pub fn register(
        &self,
        machine: Uuid,
    ) -> (
        Uuid,
        oneshot::Receiver<(u16, &'static str)>,
        CommandReceiver,
    ) {
        let connection_id = Uuid::new_v4();
        let (close, receiver) = oneshot::channel();
        let (sender, commands) = mpsc::channel(64);
        let pending = Arc::new(Mutex::new(Some(HashMap::new())));
        let mut slots = self.slots.lock().expect("registry mutex poisoned");
        let old = slots
            .entry(machine)
            .or_default()
            .current
            .replace(ChannelHandle {
                connection_id,
                close,
                live: false,
                commands: CommandSender {
                    sender,
                    pending: pending.clone(),
                },
            });
        if let Some(old) = old {
            // Publish replacement and its signal under the same lock.
            old.commands.close();
            let _ = old.close.send((4409, "replaced"));
        }
        (
            connection_id,
            receiver,
            CommandReceiver {
                receiver: commands,
                pending,
            },
        )
    }
    pub fn remove(&self, machine: Uuid, connection: Uuid) {
        let mut slots = self.slots.lock().expect("registry mutex poisoned");
        if let Some(slot) = slots.get_mut(&machine)
            && slot
                .current
                .as_ref()
                .is_some_and(|entry| entry.connection_id == connection)
        {
            if let Some(current) = &slot.current {
                current.commands.close();
            }
            slot.current = None;
        }
    }
    pub fn mark_live(&self, machine: Uuid, connection: Uuid) -> bool {
        let mut slots = self.slots.lock().expect("registry mutex poisoned");
        if let Some(entry) = slots
            .get_mut(&machine)
            .and_then(|slot| slot.current.as_mut())
            && entry.connection_id == connection
        {
            entry.live = true;
            return true;
        }
        false
    }
    pub fn is_connected(&self, machine: Uuid) -> bool {
        self.slots
            .lock()
            .expect("registry mutex poisoned")
            .get(&machine)
            .and_then(|slot| slot.current.as_ref())
            .is_some_and(|entry| entry.live)
    }
    pub fn command_sender(&self, machine: Uuid) -> Option<CommandSender> {
        self.slots
            .lock()
            .expect("registry mutex poisoned")
            .get(&machine)?
            .current
            .as_ref()
            .filter(|entry| entry.live)
            .map(|entry| entry.commands.clone())
    }
    pub fn connected_ids(&self) -> Vec<Uuid> {
        self.slots
            .lock()
            .expect("registry mutex poisoned")
            .iter()
            .filter_map(|(id, slot)| {
                slot.current
                    .as_ref()
                    .filter(|entry| entry.live)
                    .map(|_| *id)
            })
            .collect()
    }
    pub fn connection_id(&self, machine: Uuid) -> Option<Uuid> {
        self.slots
            .lock()
            .expect("registry mutex poisoned")
            .get(&machine)
            .and_then(|slot| slot.current.as_ref())
            .map(|entry| entry.connection_id)
    }
    pub async fn persist<F: Future>(
        &self,
        machine: Uuid,
        connection: Uuid,
        write: F,
    ) -> PersistOutcome<F::Output> {
        let gate = self
            .slots
            .lock()
            .expect("registry mutex poisoned")
            .get(&machine)
            .map(|slot| slot.gate.clone());
        let Some(gate) = gate else {
            return PersistOutcome::Skipped;
        };
        let _guard = gate.lock().await;
        if self.connection_id(machine) != Some(connection) {
            return PersistOutcome::Skipped;
        }
        PersistOutcome::Written(write.await)
    }
}
