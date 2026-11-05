//! Test-only counters for the explicit-file visibility path.
//!
//! Counters are thread-local, so a test sees only the work its own thread did.

use std::cell::Cell;

thread_local! {
    static VISIBILITY_WALKS: Cell<usize> = const { Cell::new(0) };
    static HIDDEN_CONTEXT_LOADS: Cell<usize> = const { Cell::new(0) };
}

/// Visibility work recorded on the current thread since the last [`reset`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct OperationCounts {
    /// Filesystem walkers built to decide whether explicit paths are visible.
    pub(crate) visibility_walks: usize,
    /// Hidden-path contexts loaded from a project root.
    pub(crate) hidden_context_loads: usize,
}

pub(super) fn record_visibility_walk() {
    VISIBILITY_WALKS.with(|count| count.set(count.get() + 1));
}

pub(super) fn record_hidden_context_load() {
    HIDDEN_CONTEXT_LOADS.with(|count| count.set(count.get() + 1));
}

pub(crate) fn reset() {
    VISIBILITY_WALKS.with(|count| count.set(0));
    HIDDEN_CONTEXT_LOADS.with(|count| count.set(0));
}

pub(crate) fn snapshot() -> OperationCounts {
    OperationCounts {
        visibility_walks: VISIBILITY_WALKS.with(Cell::get),
        hidden_context_loads: HIDDEN_CONTEXT_LOADS.with(Cell::get),
    }
}
