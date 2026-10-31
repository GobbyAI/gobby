//! Arrival-ordered turns for one run's cancel and write commands. A command takes its ticket
//! when the channel admits it, and runs once every earlier command for the same run finishes.
use std::{
    collections::HashMap,
    sync::{Arc, Mutex, PoisonError},
};
use tokio::sync::watch;

/// The lanes of every run with a command in flight, keyed by run id.
#[derive(Default)]
pub struct Lanes(Mutex<HashMap<String, Lane>>);

struct Lane {
    /// The most recent ticket handed out for this run.
    admitted: u64,
    /// The highest ticket whose command has finished.
    finished: Arc<watch::Sender<u64>>,
}

/// One command's place in its run's lane. Dropping the turn finishes the command.
pub struct Turn {
    lanes: Arc<Lanes>,
    run_id: String,
    ticket: u64,
    finished: Arc<watch::Sender<u64>>,
}

impl Lanes {
    /// Takes the next ticket for `run_id`. Call in arrival order, before the command awaits.
    pub fn enter(self: &Arc<Self>, run_id: &str) -> Turn {
        let mut lanes = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        let lane = lanes.entry(run_id.to_owned()).or_insert_with(|| Lane {
            admitted: 0,
            finished: Arc::new(watch::channel(0).0),
        });
        lane.admitted += 1;
        Turn {
            lanes: Arc::clone(self),
            run_id: run_id.to_owned(),
            ticket: lane.admitted,
            finished: Arc::clone(&lane.finished),
        }
    }
}

impl Turn {
    /// Waits until every command admitted before this one for the same run has finished.
    pub async fn wait(&self) {
        // Tickets are consecutive, so this command's turn comes when its predecessor finishes.
        let _ = self
            .finished
            .subscribe()
            .wait_for(|finished| *finished + 1 >= self.ticket)
            .await;
    }
}

impl Drop for Turn {
    fn drop(&mut self) {
        self.finished.send_replace(self.ticket);
        let mut lanes = self.lanes.0.lock().unwrap_or_else(PoisonError::into_inner);
        if lanes
            .get(&self.run_id)
            .is_some_and(|lane| lane.admitted == self.ticket)
        {
            lanes.remove(&self.run_id);
        }
    }
}
