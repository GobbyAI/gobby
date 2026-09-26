//! The terminal relist. A lagged receiver's relist runs beside the live loop,
//! so a daemon slow to answer cannot hold it while frames lag it again
//! (#22747). The relist's snapshot pins the lifecycle order, so an answer that
//! something newer overtook is not installed.

use super::*;

/// Whether a relist is due, and how many relists were installed: a background
/// answer counts only when none was installed after its request started.
#[derive(Debug, Default)]
pub(crate) struct RelistState {
    pending: bool,
    generation: u64,
}

/// The rows and snapshot one background relist came back with.
pub(crate) struct Relist {
    generation: u64,
    project: String,
    rows: Vec<TerminalRow>,
    pin: Snapshot,
}

/// A running background relist; the live loop polls it from a select branch.
pub(crate) type RelistFuture = Pin<Box<dyn Future<Output = Result<Relist, DaemonError>> + 'static>>;

impl Workspace<LiveDaemon> {
    /// Relists inline, replacing the roster and the lifecycle pin.
    pub async fn fetch_roster(&mut self) -> Result<(), DaemonError> {
        let project = self.project_id.clone().unwrap_or_default();
        let (rows, pin) = list_roster(&self.daemon, &project).await?;
        self.install_relist(rows, pin);
        // Newer than any relist still due or in flight.
        self.relist.pending = false;
        Ok(())
    }

    /// Marks a relist due; the live loop runs it beside itself.
    pub(super) fn request_relist(&mut self) {
        self.relist.pending = true;
    }

    /// Starts the relist a lagged receiver asked for, if one is due.
    pub(crate) fn start_relist(&mut self) -> Option<RelistFuture> {
        if !std::mem::take(&mut self.relist.pending) {
            return None;
        }
        let daemon = self.daemon.clone();
        let project = self.project_id.clone().unwrap_or_default();
        let generation = self.relist.generation;
        Some(Box::pin(async move {
            let (rows, pin) = list_roster(&daemon, &project).await?;
            Ok(Relist {
                generation,
                project,
                rows,
                pin,
            })
        }))
    }

    pub(crate) fn start_initial_relist(&mut self) -> RelistFuture {
        self.request_relist();
        self.start_relist().expect("initial relist was requested")
    }

    /// Installs a background relist unless something newer overtook it.
    pub(crate) fn apply_relist(&mut self, relist: Relist) {
        if relist.generation != self.relist.generation
            || self.project_id.as_deref().unwrap_or_default() != relist.project
        {
            return;
        }
        // A lifecycle event applied while the request was out is newer than
        // the answer, and installing it would undo the event: ask again.
        if self.lifecycle.as_ref().is_some_and(|pin| {
            pin.daemon_epoch == relist.pin.daemon_epoch && pin.seq > relist.pin.seq
        }) {
            self.relist.pending = true;
            return;
        }
        self.install_relist(relist.rows, relist.pin);
    }

    fn install_relist(&mut self, rows: Vec<TerminalRow>, pin: Snapshot) {
        self.install_live_rows(rows);
        self.lifecycle = Some(pin);
        self.relist.generation += 1;
    }
}

/// Every terminal of `project` and the snapshot its first page carried.
async fn list_roster(
    daemon: &LiveDaemon,
    project: &str,
) -> Result<(Vec<TerminalRow>, Snapshot), DaemonError> {
    for attempt in 0..2 {
        let mut cursor: Option<String> = None;
        let mut cursors = HashSet::new();
        let mut rows = Vec::new();
        let mut known = HashSet::new();
        let mut pin = Snapshot::default();
        let result = loop {
            let page = match daemon.list_terminals(project, cursor.as_deref()).await {
                Ok(page) => page,
                Err(error) => break Err(error),
            };
            if cursor.is_none() {
                let Some(snapshot) = page.snapshot else {
                    break Err(DaemonError::Protocol {
                        detail: "first terminal page omitted snapshot".into(),
                    });
                };
                pin = snapshot;
            }
            for row in page.items {
                if !row.id().is_empty() && known.insert(row.id().to_string()) {
                    rows.push(row);
                }
            }
            match page.next_cursor.filter(|next| !next.is_empty()) {
                Some(next) if cursors.insert(next.clone()) => cursor = Some(next),
                Some(_) => {
                    break Err(DaemonError::Protocol {
                        detail: "terminal cursor repeated".into(),
                    });
                }
                None => break Ok(()),
            }
        };
        match result {
            Ok(()) => return Ok((rows, pin)),
            Err(error) if attempt == 0 && is_cursor_error(&error) => continue,
            Err(error) => return Err(error),
        }
    }
    Err(DaemonError::Protocol {
        detail: "terminal pagination did not converge".into(),
    })
}
