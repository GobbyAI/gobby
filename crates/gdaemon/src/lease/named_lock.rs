//! The lease lock, with a record of what its current holder is doing, so that a task waiting on
//! it can name the holder when it gives up.

use std::ops::{Deref, DerefMut};
use std::sync::{Mutex as StdMutex, PoisonError};

use tokio::sync::{Mutex, MutexGuard};

/// The holder label while the lock is free.
const FREE: &str = "no holder";

/// A tokio mutex that records the label of its current holder until that holder releases it.
pub struct NamedLock<T> {
    lock: Mutex<T>,
    holder: StdMutex<&'static str>,
}

impl<T> NamedLock<T> {
    pub fn new(value: T) -> Self {
        Self {
            lock: Mutex::new(value),
            holder: StdMutex::new(FREE),
        }
    }

    /// Takes the lock, recording `holder` as its holder until the guard drops.
    pub async fn lock_as(&self, holder: &'static str) -> NamedGuard<'_, T> {
        let guard = self.lock.lock().await;
        *self.holder.lock().unwrap_or_else(PoisonError::into_inner) = holder;
        NamedGuard { guard, lock: self }
    }

    /// The label of the current holder, or a placeholder while the lock is free.
    pub fn holder(&self) -> &'static str {
        *self.holder.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

/// Holds a [`NamedLock`] and clears its holder label when dropped.
pub struct NamedGuard<'a, T> {
    guard: MutexGuard<'a, T>,
    lock: &'a NamedLock<T>,
}

impl<T> Deref for NamedGuard<'_, T> {
    type Target = T;

    fn deref(&self) -> &T {
        &self.guard
    }
}

impl<T> DerefMut for NamedGuard<'_, T> {
    fn deref_mut(&mut self) -> &mut T {
        &mut self.guard
    }
}

impl<T> Drop for NamedGuard<'_, T> {
    fn drop(&mut self) {
        // The label clears before the lock releases, so no waiter reads a stale holder.
        *self
            .lock
            .holder
            .lock()
            .unwrap_or_else(PoisonError::into_inner) = FREE;
    }
}
