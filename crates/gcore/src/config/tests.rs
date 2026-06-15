use super::*;
use std::collections::HashMap;
use std::sync::{Mutex, MutexGuard, Once};

struct TestLogger {
    records: Mutex<Vec<String>>,
}

static TEST_LOGGER: TestLogger = TestLogger {
    records: Mutex::new(Vec::new()),
};
static TEST_LOGGER_INIT: Once = Once::new();

impl TestLogger {
    fn clear(&self) {
        self.lock_records().clear();
    }

    fn records(&self) -> Vec<String> {
        self.lock_records().clone()
    }

    fn lock_records(&self) -> std::sync::MutexGuard<'_, Vec<String>> {
        self.records
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

impl log::Log for TestLogger {
    fn enabled(&self, metadata: &log::Metadata<'_>) -> bool {
        metadata.level() <= log::Level::Warn
    }

    fn log(&self, record: &log::Record<'_>) {
        if self.enabled(record.metadata()) {
            self.lock_records()
                .push(format!("{}: {}", record.level(), record.args()));
        }
    }

    fn flush(&self) {}
}

fn capture_warn_logs<R>(f: impl FnOnce() -> R) -> (R, Vec<String>) {
    TEST_LOGGER_INIT.call_once(|| {
        log::set_logger(&TEST_LOGGER).expect("install test logger");
        log::set_max_level(log::LevelFilter::Warn);
    });
    TEST_LOGGER.clear();
    let result = f();
    (result, TEST_LOGGER.records())
}

// All process-environment mutation in this module must happen through
// EnvGuard, which holds TEST_ENV_LOCK for the full test scope.
struct EnvGuard {
    lock: MutexGuard<'static, ()>,
}

impl EnvGuard {
    fn new() -> Self {
        let guard = Self {
            lock: TEST_ENV_LOCK
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner()),
        };
        guard.clear();
        guard
    }

    fn clear(&self) {
        let _held_env_lock = &self.lock;
        for key in [
            "GOBBY_FALKORDB_HOST",
            "GOBBY_FALKORDB_PORT",
            "GOBBY_FALKORDB_PASSWORD",
            "GOBBY_QDRANT_URL",
            "GOBBY_QDRANT_API_KEY",
            "GOBBY_INDEXING_RESPECT_GITIGNORE",
            "GOBBY_TEST_PRESENT",
            "GOBBY_TEST_MISSING",
        ] {
            // SAFETY: TEST_ENV_LOCK must guard every test environment mutation
            // in this crate. This loop only removes the fixed key list above.
            unsafe { std::env::remove_var(key) };
        }
    }

    fn set(&self, key: &str, value: &str) {
        let _held_env_lock = &self.lock;
        unsafe { std::env::set_var(key, value) };
    }
}

impl Drop for EnvGuard {
    fn drop(&mut self) {
        self.clear();
    }
}

#[derive(Default)]
struct TestSource {
    values: HashMap<&'static str, String>,
    resolved_values: Vec<String>,
}

impl TestSource {
    fn with_values(values: impl IntoIterator<Item = (&'static str, &'static str)>) -> Self {
        Self {
            values: values
                .into_iter()
                .map(|(key, value)| (key, value.to_string()))
                .collect(),
            resolved_values: Vec::new(),
        }
    }

    fn with_raw_values(values: impl IntoIterator<Item = (&'static str, &'static str)>) -> Self {
        Self {
            values: values
                .into_iter()
                .filter_map(|(key, value)| decode_config_value(value).map(|v| (key, v)))
                .collect(),
            resolved_values: Vec::new(),
        }
    }
}

impl ConfigSource for TestSource {
    fn config_value(&mut self, key: &str) -> Option<String> {
        self.values.get(key).cloned()
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        self.resolved_values.push(value.to_string());
        if let Some(secret_name) = value.strip_prefix("$secret:") {
            return Ok(format!("resolved-{secret_name}"));
        }
        Ok(resolve_env_pattern(value)?.unwrap_or_else(|| value.to_string()))
    }
}

#[derive(Default)]
struct FailingResolveSource {
    values: HashMap<&'static str, String>,
    failing_values: Vec<String>,
}

impl FailingResolveSource {
    fn with_values_and_failures(
        values: impl IntoIterator<Item = (&'static str, &'static str)>,
        failing_values: impl IntoIterator<Item = &'static str>,
    ) -> Self {
        Self {
            values: values
                .into_iter()
                .map(|(key, value)| (key, value.to_string()))
                .collect(),
            failing_values: failing_values.into_iter().map(str::to_string).collect(),
        }
    }
}

impl ConfigSource for FailingResolveSource {
    fn config_value(&mut self, key: &str) -> Option<String> {
        self.values.get(key).cloned()
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        if self.failing_values.iter().any(|failing| failing == value) {
            return Err(anyhow::anyhow!("resolver failed"));
        }
        Ok(resolve_env_pattern(value)?.unwrap_or_else(|| value.to_string()))
    }
}

#[derive(Default)]
struct LayeredTestSource {
    store: TestSource,
    yaml: TestSource,
}

impl LayeredTestSource {
    fn with_layers(
        store_values: impl IntoIterator<Item = (&'static str, &'static str)>,
        yaml_values: impl IntoIterator<Item = (&'static str, &'static str)>,
    ) -> Self {
        Self {
            store: TestSource::with_values(store_values),
            yaml: TestSource::with_values(yaml_values),
        }
    }
}

impl ConfigSource for LayeredTestSource {
    fn config_value(&mut self, key: &str) -> Option<String> {
        self.store
            .config_value(key)
            .or_else(|| self.yaml.config_value(key))
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        self.store.resolve_value(value)
    }
}

mod ai;
mod embedding_guard;
mod indexing;
mod resolution;
