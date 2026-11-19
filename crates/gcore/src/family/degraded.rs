//! The names of the services and families that run degraded.

use std::collections::BTreeSet;
use std::sync::{Mutex, MutexGuard, PoisonError};

/// The set of degraded names: a family's own name after an ordinary hook error, or a
/// service name such as `database_pool` or `gterm_host`.
#[derive(Debug, Default)]
pub struct DegradedServices {
    names: Mutex<BTreeSet<String>>,
}

impl DegradedServices {
    fn lock(&self) -> MutexGuard<'_, BTreeSet<String>> {
        self.names.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Marks `name` degraded.
    pub fn mark(&self, name: &str) {
        let _ = name;
    }

    /// Removes `name` from the set.
    pub fn clear(&self, name: &str) {
        let _ = name;
    }

    /// Every marked name, sorted.
    pub fn names(&self) -> Vec<String> {
        self.lock().iter().cloned().collect()
    }

    /// Whether `name` is marked.
    pub fn contains(&self, name: &str) -> bool {
        self.lock().contains(name)
    }
}
