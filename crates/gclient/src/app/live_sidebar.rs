//! Initial sidebar loading, refetch scheduling, and row installation.

use super::*;

/// Jittered exponential backoff for one sidebar query that a failure
/// re-queues: every attempt pushes the next one out, and a success resets it.
#[derive(Default)]
pub(in crate::app) struct FetchRetry {
    failures: u8,
    next_at: Option<Instant>,
}

impl FetchRetry {
    fn ready(&self, now: Instant) -> bool {
        self.next_at.is_none_or(|next| now >= next)
    }

    /// An attempt started and has not succeeded since.
    fn unsettled(&self) -> bool {
        self.failures > 0
    }

    fn started(&mut self, now: Instant, jitter: u64) {
        self.failures = self.failures.saturating_add(1).min(6);
        let ceiling = 1_u64 << self.failures;
        let lower = ceiling / 2;
        self.next_at = Some(now + std::time::Duration::from_secs(lower + jitter % (lower + 1)));
    }

    fn succeeded(&mut self) {
        self.failures = 0;
        self.next_at = None;
    }
}

impl Workspace<LiveDaemon> {
    pub fn roster_refresh_age(&self) -> Option<std::time::Duration> {
        self.last_roster_refresh_completed_at.map(|at| at.elapsed())
    }

    /// Replace the roster wholesale: an entry the daemon no longer returns is
    /// gone, whatever an event said about it.
    pub(super) async fn fetch_attention(&mut self) -> Result<(), DaemonError> {
        let roster = self.daemon.attention_roster().await?;
        // A background roster refetch started earlier lands stale, and one
        // queued but not yet started has its answer — including the next one
        // the interval would have asked for.
        self.sidebar_stamps.roster = self.sidebar_stamps.next();
        self.pending_sidebar.roster = false;
        self.roster_refreshed_at = Instant::now();
        self.install_attention(roster);
        Ok(())
    }

    pub(crate) fn install_attention(&mut self, (epoch, seq, entries): RosterSnapshot) {
        self.last_roster_refresh_completed_at = Some(Instant::now());
        self.attention.epoch = epoch;
        self.attention.seq = seq;
        self.attention.entries = entries;
        self.attention.applied_seqs = vec![seq];
        self.rebuild_sidebar();
    }

    /// Fetch every sidebar row: projects, then status and worktrees for each
    /// project checked out here, then the sessions and runs of every
    /// checked-out project and the focused one. Reconcile and the dialogs
    /// wait on this; the render tick never does.
    pub async fn fetch_sidebar_rows(&mut self) -> Result<(), DaemonError> {
        self.sidebar_rows.projects = self.daemon.projects().await?;
        self.sidebar_rows.statuses.clear();
        self.sidebar_rows.worktrees.clear();
        // Every row set is fresh from here: a refetch started earlier and
        // still in flight lands stale.
        let seq = self.sidebar_stamps.next();
        self.sidebar_stamps.stamp_all(seq);
        self.pending_sidebar = PendingSidebar {
            projects: false,
            project_rows: self.checked_out_projects().into_iter().collect(),
            sessions: true,
            session_rows: BTreeSet::new(),
            roster: false,
        };
        self.flush_sidebar_refetches().await
    }

    /// Queue the initial sidebar fan-out beside the live loop. The first
    /// workspace frame and input must not wait for project or git endpoints.
    pub fn queue_initial_sidebar_fetch(&mut self) {
        self.pending_sidebar.projects = true;
        self.pending_sidebar.sessions = true;
        self.pending_sidebar.roster = true;
        self.pending_sidebar
            .project_rows
            .extend(self.checked_out_projects());
    }

    pub(super) fn checked_out_projects(&self) -> Vec<String> {
        self.sidebar_rows
            .projects
            .iter()
            .filter(|project| project.checkout.is_some())
            .map(|project| project.id.clone())
            .collect()
    }

    /// Queue a refetch of every checked-out project's sessions and runs (the
    /// focused one included); the next render tick starts it.
    pub fn request_focused_sessions(&mut self) {
        self.pending_sidebar.sessions = true;
    }

    /// Queue again what a refetch that gathered nothing had asked for. Only a
    /// project event asks for the project list otherwise, so without this a
    /// list that failed at startup stays empty until a project changes.
    pub fn requeue_failed_sidebar_fetch(&mut self) {
        self.request_focused_sessions();
        self.pending_sidebar.projects |= self.projects_retry.unsettled();
    }

    /// Start the refetches queued since the last start, at most one per
    /// route and project however many events asked for it, on a clone of
    /// the daemon so the loop keeps drawing while they run; `None` when
    /// nothing is queued. The git rows count as refreshed from this moment,
    /// so a refresh that fails waits out `GIT_REFRESH_INTERVAL` instead of
    /// retrying every tick. `apply_sidebar_fetch` installs the result.
    pub fn start_sidebar_refetch(&mut self) -> Option<SidebarFetchFuture> {
        let mut pending = std::mem::take(&mut self.pending_sidebar);
        let now = Instant::now();
        if !self.session_retry.ready(now) {
            self.pending_sidebar.sessions = pending.sessions;
            self.pending_sidebar.session_rows = std::mem::take(&mut pending.session_rows);
            pending.sessions = false;
        }
        if !self.projects_retry.ready(now) {
            self.pending_sidebar.projects = pending.projects;
            pending.projects = false;
        }
        if !pending.projects
            && !pending.sessions
            && !pending.roster
            && pending.project_rows.is_empty()
            && pending.session_rows.is_empty()
        {
            return None;
        }
        if !pending.project_rows.is_empty() {
            self.git_refreshed_at = Instant::now();
        }
        if pending.roster {
            self.roster_refreshed_at = Instant::now();
        }
        if pending.sessions || !pending.session_rows.is_empty() {
            self.session_retry
                .started(now, uuid::Uuid::new_v4().as_u128() as u64);
        }
        if pending.projects {
            self.projects_retry
                .started(now, uuid::Uuid::new_v4().as_u128() as u64);
        }
        let request = SidebarRequest {
            seq: self.sidebar_stamps.next(),
            projects: pending.projects,
            project_rows: pending.project_rows,
            checked_out: self.checked_out_projects(),
            sessions: pending.sessions,
            session_rows: pending.session_rows,
            focused: self.project_id.clone(),
            roster: pending.roster,
        };
        let daemon = self.daemon.clone();
        Some(Box::pin(async move { request.run(&daemon).await }))
    }

    /// Install the rows one `start_sidebar_refetch` job produced, row set by
    /// row set: a set a later refetch already replaced is stale and stays
    /// out.
    pub fn apply_sidebar_fetch(&mut self, fetch: SidebarFetch) {
        if fetch.sessions_attempted {
            if fetch.sessions_failed {
                self.pending_sidebar.sessions = true;
            } else {
                self.session_retry.succeeded();
            }
        }
        if fetch.projects_attempted {
            if fetch.projects.is_some() {
                self.projects_retry.succeeded();
            } else {
                self.pending_sidebar.projects = true;
            }
        }
        let seq = fetch.seq;
        let stamps = &mut self.sidebar_stamps;
        if let Some(projects) = fetch.projects {
            if SidebarStamps::accept(&mut stamps.projects, seq) {
                self.sidebar_rows.projects = projects;
            }
        }
        for (project, status, worktrees) in fetch.project_rows {
            let stamp = stamps.project_rows.entry(project.clone()).or_default();
            if !SidebarStamps::accept(stamp, seq) {
                continue;
            }
            if let Some(worktrees) = worktrees {
                self.sidebar_rows
                    .worktrees
                    .retain(|row| row.project_id != project);
                self.sidebar_rows.worktrees.extend(worktrees);
            }
            if let Some(status) = status {
                self.sidebar_rows.statuses.insert(project, status);
            }
        }
        for (project, sessions, runs) in fetch.sessions {
            let stamp = stamps.sessions.entry(project.clone()).or_default();
            if SidebarStamps::accept(stamp, seq) {
                self.sidebar_rows.sessions.insert(project.clone(), sessions);
                self.sidebar_rows.runs.insert(project, runs);
            }
        }
        if let Some(roster) = fetch.roster {
            if SidebarStamps::accept(&mut stamps.roster, seq) {
                let (epoch, roster_seq, _) = &roster;
                if *epoch == self.attention.epoch && *roster_seq < self.attention.seq {
                    // An event newer than this snapshot already landed;
                    // installing it would undo that event, so ask again.
                    self.pending_sidebar.roster = true;
                } else {
                    self.install_attention(roster);
                }
            }
        }
        self.rebuild_sidebar();
    }

    /// Run the queued refetches inline and install them, for the reconcile
    /// and drain paths where a stale sidebar would be worse than the wait.
    pub async fn flush_sidebar_refetches(&mut self) -> Result<(), DaemonError> {
        if let Some(job) = self.start_sidebar_refetch() {
            let fetch = job.await?;
            self.apply_sidebar_fetch(fetch);
        }
        Ok(())
    }

    /// Queue a status refresh for every checked-out project once the last
    /// one is older than `GIT_REFRESH_INTERVAL`; the render tick starts it.
    pub fn request_git_refresh_if_due(&mut self) {
        if self.git_refreshed_at.elapsed() < GIT_REFRESH_INTERVAL {
            return;
        }
        self.pending_sidebar
            .project_rows
            .extend(self.checked_out_projects());
    }

    /// Queues a roster, session and run refetch once the last roster fetch is
    /// older than `ROSTER_REFRESH_INTERVAL`; the render tick starts it. Events
    /// refetch sooner; this is the backstop for a missed event or failed fetch.
    pub fn request_roster_refresh_if_due(&mut self) {
        if self.roster_refreshed_at.elapsed() < ROSTER_REFRESH_INTERVAL {
            return;
        }
        self.pending_sidebar.roster = true;
        self.pending_sidebar.sessions = true;
    }

    /// Queue the refetch a project, worktree, or session event calls for;
    /// `flush_sidebar_refetches` runs each at most once per drain.
    pub(super) fn note_sidebar_message(&mut self, message: &Value) {
        let project_id = message
            .get("project_id")
            .and_then(Value::as_str)
            .map(str::to_string);
        match message_kind(message) {
            Some("project_event") => {
                self.pending_sidebar.projects = true;
                if let Some(project_id) = project_id {
                    self.pending_sidebar.project_rows.insert(project_id);
                }
            }
            Some("worktree_event") => match project_id {
                Some(project_id) => {
                    self.pending_sidebar.project_rows.insert(project_id);
                }
                // A worktree event without its project refreshes every
                // checkout rather than guessing which one moved.
                None => self
                    .pending_sidebar
                    .project_rows
                    .extend(self.checked_out_projects()),
            },
            Some("session_event") => {
                match project_id {
                    Some(project_id) => {
                        self.pending_sidebar.session_rows.insert(project_id);
                    }
                    // A session event without its project refetches every
                    // tracked project rather than guessing which one
                    // changed: a deleted session leaves no row to name it.
                    None => self.pending_sidebar.sessions = true,
                }
                self.pending_sidebar.roster = true;
            }
            _ => {}
        }
    }
}

/// Keeps one sidebar row query's failure from ending the refetch: the error is
/// logged, remembered for the caller's banner, and reported as an absent row.
fn optional<T>(
    result: Result<T, DaemonError>,
    what: &'static str,
    project: Option<&str>,
    failure: &mut Option<DaemonError>,
) -> Option<T> {
    match result {
        Ok(value) => Some(value),
        Err(error) => {
            // The project belongs in the line: which one went sick is what a
            // stale row is diagnosed from.
            tracing::warn!(
                %what,
                project = project.unwrap_or(""),
                %error,
                "sidebar row refresh failed"
            );
            failure.get_or_insert(error);
            None
        }
    }
}

/// Rows one background sidebar refetch produced; `apply_sidebar_fetch`
/// installs them.
#[derive(Debug, Default)]
pub struct SidebarFetch {
    /// The refetch that produced the rows, in start order.
    seq: u64,
    projects: Option<Vec<ProjectRow>>,
    projects_attempted: bool,
    project_rows: Vec<(String, Option<SourceStatus>, Option<Vec<WorktreeRow>>)>,
    sessions: Vec<(String, Vec<SessionRow>, Vec<RunRow>)>,
    sessions_attempted: bool,
    sessions_failed: bool,
    roster: Option<RosterSnapshot>,
}

/// The attention roster as the daemon returned it: epoch, seq, entries.
type RosterSnapshot = (String, u64, Vec<RosterEntry>);

/// A running sidebar refetch; the live loop polls it from a select branch.
pub type SidebarFetchFuture =
    Pin<Box<dyn Future<Output = Result<SidebarFetch, DaemonError>> + 'static>>;

/// What one refetch asks the daemon for.
struct SidebarRequest {
    seq: u64,
    projects: bool,
    project_rows: BTreeSet<String>,
    /// The projects checked out here when the request was made; a refetched
    /// project list replaces it. A project with no checkout has no git rows.
    checked_out: Vec<String>,
    /// Whether the sessions and runs of every checked-out project (and the
    /// focused one) are wanted: the roster's entries join their own
    /// project, so the all-projects scope lists them where they belong.
    sessions: bool,
    /// The projects whose sessions and runs one named event each asked for,
    /// fetched instead of the sweep when `sessions` is false. A project this
    /// client does not track is dropped: its rows have nowhere to go.
    session_rows: BTreeSet<String>,
    focused: Option<String>,
    roster: bool,
}

impl SidebarRequest {
    async fn run(self, daemon: &LiveDaemon) -> Result<SidebarFetch, DaemonError> {
        let mut fetch = SidebarFetch {
            seq: self.seq,
            ..SidebarFetch::default()
        };
        // No row query starves another. Each records its own failure and the
        // refetch fails only when it gathered nothing at all, so a daemon that is
        // really gone still raises the banner while one sick project cannot empty
        // the sidebar. The roster runs first because it names the panes a person
        // can reach, and it must never wait behind a project's git status.
        let mut failure = None;
        let mut gathered = false;
        if self.roster {
            if let Some(roster) = optional(
                daemon.attention_roster().await,
                "attention roster",
                None,
                &mut failure,
            ) {
                fetch.roster = Some(roster);
                gathered = true;
            }
        }
        let mut checked_out = self.checked_out;
        let mut project_rows = self.project_rows;
        if self.projects {
            fetch.projects_attempted = true;
            // A project list this refetch could not read leaves the caller's
            // known checkouts standing in for it.
            if let Some(projects) =
                optional(daemon.projects().await, "projects", None, &mut failure)
            {
                checked_out = projects
                    .iter()
                    .filter(|row| row.checkout.is_some())
                    .map(|row| row.id.clone())
                    .collect();
                project_rows.extend(checked_out.iter().cloned());
                fetch.projects = Some(projects);
                gathered = true;
            }
        }
        for project in project_rows {
            if !checked_out.contains(&project) {
                continue;
            }
            let status = optional(
                daemon.source_status(&project).await,
                "source status",
                Some(&project),
                &mut failure,
            );
            let worktrees = optional(
                daemon.worktrees(&project).await,
                "worktrees",
                Some(&project),
                &mut failure,
            );
            if status.is_some() || worktrees.is_some() {
                fetch.project_rows.push((project, status, worktrees));
                gathered = true;
            }
        }
        if self.sessions || !self.session_rows.is_empty() {
            fetch.sessions_attempted = true;
            let mut projects = checked_out.clone();
            if let Some(focused) = self.focused.filter(|focused| !projects.contains(focused)) {
                projects.push(focused);
            }
            if !self.sessions {
                projects.retain(|project| self.session_rows.contains(project));
            }
            for project in projects {
                // A project whose rows this refetch could not read keeps the ones
                // it already has, and the rest of the sidebar still refreshes.
                let Some(sessions) = optional(
                    daemon.sessions(&project).await,
                    "sessions",
                    Some(&project),
                    &mut failure,
                ) else {
                    fetch.sessions_failed = true;
                    continue;
                };
                let Some(runs) = optional(
                    daemon.agent_runs(&project).await,
                    "agent runs",
                    Some(&project),
                    &mut failure,
                ) else {
                    fetch.sessions_failed = true;
                    continue;
                };
                fetch.sessions.push((project, sessions, runs));
                gathered = true;
            }
        }
        match failure {
            Some(error) if !gathered => Err(error),
            _ => Ok(fetch),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fetch_retry_uses_jittered_exponential_delay_and_resets_after_success() {
        let now = Instant::now();
        let mut retry = FetchRetry::default();
        for exponent in 1..=6 {
            retry.started(now, 0);
            let lower = 1_u64 << (exponent - 1);
            assert!(!retry.ready(now + std::time::Duration::from_secs(lower - 1)));
            assert!(retry.ready(now + std::time::Duration::from_secs(lower)));
        }
        retry.started(now, 32);
        assert!(!retry.ready(now + std::time::Duration::from_secs(63)));
        assert!(retry.ready(now + std::time::Duration::from_secs(64)));
        retry.succeeded();
        assert!(retry.ready(now));
        retry.started(now, 0);
        assert!(retry.ready(now + std::time::Duration::from_secs(1)));
    }
}
