use std::thread;
use std::time::{Duration, Instant};

use postgres::Client;
use sha2::{Digest, Sha256};

use crate::WikiError;
use crate::support::postgres::require_postgres_index_readwrite;

const WRITER_WAIT: Duration = Duration::from_secs(1_800);
const WRITER_POLL: Duration = Duration::from_millis(250);
const PRUNE_WAIT: Duration = Duration::from_millis(150);
const PRUNE_POLL: Duration = Duration::from_millis(25);
const PROJECT_STATE_SQL: &str = "SELECT deleted_at IS NULL FROM projects WHERE id::text = $1";

pub(crate) fn project_lock_key(project_id: &str) -> i64 {
    let mut hasher = Sha256::new();
    hasher.update(b"gwiki:project:");
    hasher.update(project_id.as_bytes());
    let digest = hasher.finalize();
    i64::from_be_bytes(
        digest[..8]
            .try_into()
            .expect("SHA-256 digest has at least 8 bytes"),
    )
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum ProjectRowState {
    Live,
    Deleted,
    Absent,
}

#[derive(Clone, Copy, Debug)]
struct LockPolicy {
    total_wait: Duration,
    poll: Duration,
}

impl LockPolicy {
    fn writer() -> Self {
        Self {
            total_wait: WRITER_WAIT,
            poll: WRITER_POLL,
        }
    }

    fn maintenance() -> Self {
        Self {
            total_wait: PRUNE_WAIT,
            poll: PRUNE_POLL,
        }
    }

    #[cfg(test)]
    fn immediate() -> Self {
        Self {
            total_wait: Duration::ZERO,
            poll: Duration::ZERO,
        }
    }
}

pub(crate) trait ProjectLockBackend {
    fn try_lock(&mut self, key: i64) -> Result<bool, String>;
    fn project_state(&mut self, project_id: &str) -> Result<ProjectRowState, String>;
    fn unlock(&mut self, key: i64) -> Result<bool, String>;
}

pub(crate) struct PostgresProjectLockBackend {
    conn: Client,
}

impl ProjectLockBackend for PostgresProjectLockBackend {
    fn try_lock(&mut self, key: i64) -> Result<bool, String> {
        let row = self
            .conn
            .query_one("SELECT pg_try_advisory_lock($1)", &[&key])
            .map_err(|error| error.to_string())?;
        row.try_get(0).map_err(|error| error.to_string())
    }

    fn project_state(&mut self, project_id: &str) -> Result<ProjectRowState, String> {
        let row = self
            .conn
            .query_opt(PROJECT_STATE_SQL, &[&project_id])
            .map_err(|error| error.to_string())?;
        let Some(row) = row else {
            return Ok(ProjectRowState::Absent);
        };
        let live = row
            .try_get::<_, bool>(0)
            .map_err(|error| error.to_string())?;
        Ok(if live {
            ProjectRowState::Live
        } else {
            ProjectRowState::Deleted
        })
    }

    fn unlock(&mut self, key: i64) -> Result<bool, String> {
        let row = self
            .conn
            .query_one("SELECT pg_advisory_unlock($1)", &[&key])
            .map_err(|error| error.to_string())?;
        row.try_get(0).map_err(|error| error.to_string())
    }
}

#[derive(Debug)]
pub(crate) struct ProjectLockGuard<B: ProjectLockBackend = PostgresProjectLockBackend> {
    backend: B,
    key: i64,
}

impl<B: ProjectLockBackend> Drop for ProjectLockGuard<B> {
    fn drop(&mut self) {
        match self.backend.unlock(self.key) {
            Ok(true) => {}
            Ok(false) => log::warn!("gwiki project advisory lock was not held during unlock"),
            Err(error) => log::warn!("failed to release gwiki project advisory lock: {error}"),
        }
    }
}

#[derive(Debug)]
#[allow(
    dead_code,
    reason = "the dependent gwiki prune task #18591 consumes this handoff API"
)]
pub(crate) enum PruneProjectLock<B: ProjectLockBackend = PostgresProjectLockBackend> {
    Busy,
    ProjectExists,
    Acquired(ProjectLockGuard<B>),
}

pub(crate) fn run_with_project_lock<B: ProjectLockBackend, T>(
    guard: Option<ProjectLockGuard<B>>,
    operation: impl FnOnce() -> T,
) -> T {
    let outcome = operation();
    drop(guard);
    outcome
}

pub(crate) fn acquire_writer_lock(
    project_id: &str,
    command: &'static str,
) -> Result<ProjectLockGuard, WikiError> {
    acquire_writer_with_backend(connect_backend(command)?, project_id, LockPolicy::writer())
}

pub(crate) fn acquire_purge_lock(project_id: &str) -> Result<ProjectLockGuard, WikiError> {
    acquire_purge_with_backend(
        connect_backend("gwiki purge")?,
        project_id,
        LockPolicy::maintenance(),
    )
}

#[allow(
    dead_code,
    reason = "the dependent gwiki prune task #18591 consumes this handoff API"
)]
pub(crate) fn try_acquire_prune_lock(project_id: &str) -> Result<PruneProjectLock, WikiError> {
    acquire_prune_with_backend(
        connect_backend("gwiki prune")?,
        project_id,
        LockPolicy::maintenance(),
    )
}

fn connect_backend(command: &'static str) -> Result<PostgresProjectLockBackend, WikiError> {
    Ok(PostgresProjectLockBackend {
        conn: require_postgres_index_readwrite(command)?,
    })
}

fn acquire_writer_with_backend<B: ProjectLockBackend>(
    backend: B,
    project_id: &str,
    policy: LockPolicy,
) -> Result<ProjectLockGuard<B>, WikiError> {
    let mut guard = acquire_required_lock(backend, project_id, policy, "writer")?;
    match guard
        .backend
        .project_state(project_id)
        .map_err(|error| backend_error("check project liveness", error))?
    {
        ProjectRowState::Live => Ok(guard),
        ProjectRowState::Deleted | ProjectRowState::Absent => Err(WikiError::PreconditionFailed {
            detail: format!(
                "gwiki project writer refused for {project_id}: no live project row exists"
            ),
        }),
    }
}

fn acquire_purge_with_backend<B: ProjectLockBackend>(
    backend: B,
    project_id: &str,
    policy: LockPolicy,
) -> Result<ProjectLockGuard<B>, WikiError> {
    acquire_required_lock(backend, project_id, policy, "purge")
}

#[allow(
    dead_code,
    reason = "the dependent gwiki prune task #18591 consumes this handoff API"
)]
fn acquire_prune_with_backend<B: ProjectLockBackend>(
    backend: B,
    project_id: &str,
    policy: LockPolicy,
) -> Result<PruneProjectLock<B>, WikiError> {
    let Some(mut guard) = acquire_lock(backend, project_id, policy)? else {
        return Ok(PruneProjectLock::Busy);
    };
    match guard
        .backend
        .project_state(project_id)
        .map_err(|error| backend_error("recheck project absence", error))?
    {
        ProjectRowState::Absent => Ok(PruneProjectLock::Acquired(guard)),
        ProjectRowState::Live | ProjectRowState::Deleted => Ok(PruneProjectLock::ProjectExists),
    }
}

fn acquire_required_lock<B: ProjectLockBackend>(
    backend: B,
    project_id: &str,
    policy: LockPolicy,
    operation: &str,
) -> Result<ProjectLockGuard<B>, WikiError> {
    acquire_lock(backend, project_id, policy)?.ok_or_else(|| WikiError::PreconditionFailed {
        detail: format!(
            "gwiki {operation} could not acquire the project lock for {project_id} within {} ms",
            policy.total_wait.as_millis()
        ),
    })
}

fn acquire_lock<B: ProjectLockBackend>(
    mut backend: B,
    project_id: &str,
    policy: LockPolicy,
) -> Result<Option<ProjectLockGuard<B>>, WikiError> {
    let key = project_lock_key(project_id);
    let started = Instant::now();
    loop {
        if backend
            .try_lock(key)
            .map_err(|error| backend_error("acquire project lock", error))?
        {
            return Ok(Some(ProjectLockGuard { backend, key }));
        }

        let elapsed = started.elapsed();
        if elapsed >= policy.total_wait {
            return Ok(None);
        }
        let remaining = policy.total_wait - elapsed;
        let sleep_for = policy.poll.min(remaining);
        if sleep_for.is_zero() {
            thread::yield_now();
        } else {
            thread::sleep(sleep_for);
        }
    }
}

fn backend_error(action: &str, error: String) -> WikiError {
    WikiError::Config {
        detail: format!("failed to {action} in PostgreSQL: {error}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::{Cell, RefCell};
    use std::collections::VecDeque;
    use std::rc::Rc;

    const PROJECT_ID_FIXTURE: &str = "d45545c5-ded5-4335-b115-0245752edacf";

    #[test]
    fn project_lock_key_matches_cross_language_fixture() {
        assert_eq!(
            project_lock_key(PROJECT_ID_FIXTURE),
            7_463_796_619_704_351_655
        );
    }

    #[test]
    fn writer_admission_requires_a_live_project_row() {
        for state in [ProjectRowState::Absent, ProjectRowState::Deleted] {
            let backend = FakeBackend::new([true], state);
            let unlocks = backend.unlocks();

            let error =
                acquire_writer_with_backend(backend, PROJECT_ID_FIXTURE, LockPolicy::immediate())
                    .expect_err("absent and deleted projects must be rejected");

            assert!(error.to_string().contains("live project"));
            assert_eq!(unlocks.get(), 1, "rejected admission releases its lock");
        }
    }

    #[test]
    fn writer_guard_releases_only_when_the_full_operation_drops_it() {
        let backend = FakeBackend::new([true], ProjectRowState::Live);
        let unlocks = backend.unlocks();

        let guard =
            acquire_writer_with_backend(backend, PROJECT_ID_FIXTURE, LockPolicy::immediate())
                .expect("live project acquires writer lock");

        let outcome = run_with_project_lock(Some(guard), || {
            assert_eq!(unlocks.get(), 0, "PostgreSQL write remains fenced");
            assert_eq!(unlocks.get(), 0, "Qdrant sync remains fenced");
            assert_eq!(unlocks.get(), 0, "Falkor sync remains fenced");
            "committed"
        });

        assert_eq!(outcome, "committed");
        assert_eq!(unlocks.get(), 1);
    }

    #[test]
    fn purge_lock_serializes_without_a_liveness_requirement() {
        let backend = FakeBackend::new([true], ProjectRowState::Deleted);
        let unlocks = backend.unlocks();

        let guard =
            acquire_purge_with_backend(backend, PROJECT_ID_FIXTURE, LockPolicy::immediate())
                .expect("explicit purge may target a soft-deleted project");

        assert_eq!(unlocks.get(), 0);
        drop(guard);
        assert_eq!(unlocks.get(), 1);
    }

    #[test]
    fn purge_fails_visibly_when_the_project_lock_is_busy() {
        let backend = FakeBackend::new([false], ProjectRowState::Deleted);

        let error =
            acquire_purge_with_backend(backend, PROJECT_ID_FIXTURE, LockPolicy::immediate())
                .expect_err("explicit purge must never interleave with a writer");

        assert!(
            error
                .to_string()
                .contains("could not acquire the project lock")
        );
    }

    #[test]
    fn prune_defers_when_the_project_lock_is_busy() {
        let backend = FakeBackend::new([false], ProjectRowState::Absent);

        let admission =
            acquire_prune_with_backend(backend, PROJECT_ID_FIXTURE, LockPolicy::immediate())
                .expect("busy is an expected prune outcome");

        assert!(matches!(admission, PruneProjectLock::Busy));
    }

    #[test]
    fn prune_retains_a_scope_when_the_project_row_appears_under_lock() {
        let backend = FakeBackend::new([true], ProjectRowState::Live);
        let unlocks = backend.unlocks();

        let admission =
            acquire_prune_with_backend(backend, PROJECT_ID_FIXTURE, LockPolicy::immediate())
                .expect("live recheck is an expected prune outcome");

        assert!(matches!(admission, PruneProjectLock::ProjectExists));
        assert_eq!(unlocks.get(), 1);
    }

    #[test]
    fn prune_holds_the_lock_while_the_project_row_remains_absent() {
        let backend = FakeBackend::new([true], ProjectRowState::Absent);
        let unlocks = backend.unlocks();

        let admission =
            acquire_prune_with_backend(backend, PROJECT_ID_FIXTURE, LockPolicy::immediate())
                .expect("absent project acquires prune lock");
        let PruneProjectLock::Acquired(guard) = admission else {
            panic!("absent project should be admitted for prune");
        };

        assert_eq!(unlocks.get(), 0);
        drop(guard);
        assert_eq!(unlocks.get(), 1);
    }

    #[derive(Clone, Debug)]
    struct FakeBackend {
        state: Rc<RefCell<FakeState>>,
        unlocks: Rc<Cell<usize>>,
    }

    #[derive(Debug)]
    struct FakeState {
        attempts: VecDeque<bool>,
        project_state: ProjectRowState,
    }

    impl FakeBackend {
        fn new(attempts: impl IntoIterator<Item = bool>, project_state: ProjectRowState) -> Self {
            Self {
                state: Rc::new(RefCell::new(FakeState {
                    attempts: attempts.into_iter().collect(),
                    project_state,
                })),
                unlocks: Rc::new(Cell::new(0)),
            }
        }

        fn unlocks(&self) -> Rc<Cell<usize>> {
            Rc::clone(&self.unlocks)
        }
    }

    impl ProjectLockBackend for FakeBackend {
        fn try_lock(&mut self, _key: i64) -> Result<bool, String> {
            Ok(self
                .state
                .borrow_mut()
                .attempts
                .pop_front()
                .unwrap_or(false))
        }

        fn project_state(&mut self, _project_id: &str) -> Result<ProjectRowState, String> {
            Ok(self.state.borrow().project_state)
        }

        fn unlock(&mut self, _key: i64) -> Result<bool, String> {
            self.unlocks.set(self.unlocks.get() + 1);
            Ok(true)
        }
    }
}
