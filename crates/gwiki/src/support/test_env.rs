use std::ffi::{OsStr, OsString};
use std::sync::MutexGuard;

/// Serializes environment mutations made through `EnvGuard` in this crate's
/// tests.
///
/// This lock only protects callers that use the helper. It cannot make global
/// process environment access safe for tests that read or write env vars
/// directly while another guarded mutation is active.
pub(crate) static ENV_TEST_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

/// Restores environment variables when dropped after a guarded test mutation.
///
/// `EnvGuard` captures each key once, holds `ENV_TEST_LOCK` for its lifetime,
/// and restores in reverse order. The safety boundary is partial: unsynchronized
/// env access outside this helper can still race guarded mutations.
pub(crate) struct EnvGuard {
    old_values: Vec<(&'static str, Option<OsString>)>,
    _lock: MutexGuard<'static, ()>,
}

impl EnvGuard {
    pub(crate) fn set(key: &'static str, value: impl AsRef<OsStr>) -> Self {
        let mut guard = Self::locked();
        guard.set_value(key, value.as_ref());
        guard
    }

    pub(crate) fn unset(key: &'static str) -> Self {
        let mut guard = Self::locked();
        guard.unset_value(key);
        guard
    }

    pub(crate) fn and_unset(mut self, key: &'static str) -> Self {
        self.unset_value(key);
        self
    }

    pub(crate) fn and_set(mut self, key: &'static str, value: impl AsRef<OsStr>) -> Self {
        self.set_value(key, value.as_ref());
        self
    }

    fn locked() -> Self {
        Self {
            old_values: Vec::new(),
            _lock: ENV_TEST_LOCK.lock().expect("env lock"),
        }
    }

    fn set_value(&mut self, key: &'static str, value: &OsStr) {
        self.capture_old_value(key);
        unsafe {
            // SAFETY: EnvGuard serializes test mutations with ENV_TEST_LOCK until Drop restores the variable.
            // It cannot prevent concurrent unsynchronized env reads outside this helper.
            std::env::set_var(key, value);
        }
    }

    fn unset_value(&mut self, key: &'static str) {
        self.capture_old_value(key);
        unsafe {
            // SAFETY: EnvGuard serializes test mutations with ENV_TEST_LOCK until Drop restores the variable.
            // It cannot prevent concurrent unsynchronized env reads outside this helper.
            std::env::remove_var(key);
        }
    }

    fn capture_old_value(&mut self, key: &'static str) {
        if self.old_values.iter().any(|(stored, _)| *stored == key) {
            return;
        }
        self.old_values.push((key, std::env::var_os(key)));
    }
}

impl Drop for EnvGuard {
    fn drop(&mut self) {
        for (key, old_value) in self.old_values.iter().rev() {
            unsafe {
                // SAFETY: EnvGuard still serializes test mutations with ENV_TEST_LOCK while restoring variables.
                // It cannot prevent concurrent unsynchronized env reads outside this helper.
                match old_value {
                    Some(value) => std::env::set_var(*key, value),
                    None => std::env::remove_var(*key),
                }
            }
        }
    }
}
