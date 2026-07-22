use std::cell::{Cell, RefCell};
use std::collections::VecDeque;
use std::rc::Rc;

use super::*;

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

    let guard = acquire_writer_with_backend(backend, PROJECT_ID_FIXTURE, LockPolicy::immediate())
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

    let guard = acquire_purge_with_backend(backend, PROJECT_ID_FIXTURE, LockPolicy::immediate())
        .expect("explicit purge may target a soft-deleted project");

    assert_eq!(unlocks.get(), 0);
    drop(guard);
    assert_eq!(unlocks.get(), 1);
}

#[test]
fn purge_fails_visibly_when_the_project_lock_is_busy() {
    let backend = FakeBackend::new([false], ProjectRowState::Deleted);

    let error = acquire_purge_with_backend(backend, PROJECT_ID_FIXTURE, LockPolicy::immediate())
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
