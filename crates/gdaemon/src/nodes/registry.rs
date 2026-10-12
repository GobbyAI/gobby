//! Lifetime machine slots fence channel publication and bookkeeping.
use std::{
    collections::HashMap,
    future::Future,
    sync::{Arc, Mutex},
};
use tokio::sync::{Mutex as AsyncMutex, oneshot};
use uuid::Uuid;

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
}

#[derive(Debug)]
pub enum PersistOutcome<T> {
    Written(T),
    Skipped,
}

impl Registry {
    pub fn register(&self, machine: Uuid) -> (Uuid, oneshot::Receiver<(u16, &'static str)>) {
        let connection_id = Uuid::new_v4();
        let (close, receiver) = oneshot::channel();
        let mut slots = self.slots.lock().expect("registry mutex poisoned");
        let old = slots
            .entry(machine)
            .or_default()
            .current
            .replace(ChannelHandle {
                connection_id,
                close,
                live: false,
            });
        if let Some(old) = old {
            // Publish replacement and its signal under the same lock.
            let _ = old.close.send((4409, "replaced"));
        }
        (connection_id, receiver)
    }
    pub fn remove(&self, machine: Uuid, connection: Uuid) {
        let mut slots = self.slots.lock().expect("registry mutex poisoned");
        if let Some(slot) = slots.get_mut(&machine)
            && slot
                .current
                .as_ref()
                .is_some_and(|entry| entry.connection_id == connection)
        {
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
