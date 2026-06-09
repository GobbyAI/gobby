use std::ffi::{OsStr, OsString};

pub(crate) static ENV_TEST_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

pub(crate) struct EnvGuard {
    key: &'static str,
    old_value: Option<OsString>,
}

impl EnvGuard {
    pub(crate) fn set(key: &'static str, value: impl AsRef<OsStr>) -> Self {
        let guard = Self {
            key,
            old_value: std::env::var_os(key),
        };
        unsafe {
            // SAFETY: env-mutating tests hold ENV_TEST_LOCK and run serially.
            std::env::set_var(key, value.as_ref());
        }
        guard
    }

    pub(crate) fn unset(key: &'static str) -> Self {
        let guard = Self {
            key,
            old_value: std::env::var_os(key),
        };
        unsafe {
            // SAFETY: env-mutating tests hold ENV_TEST_LOCK and run serially.
            std::env::remove_var(key);
        }
        guard
    }
}

impl Drop for EnvGuard {
    fn drop(&mut self) {
        unsafe {
            // SAFETY: env-mutating tests hold ENV_TEST_LOCK and run serially.
            match &self.old_value {
                Some(value) => std::env::set_var(self.key, value),
                None => std::env::remove_var(self.key),
            }
        }
    }
}
