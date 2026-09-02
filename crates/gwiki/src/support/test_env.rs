use std::ffi::{OsStr, OsString};
use std::path::Path;
use std::sync::{MutexGuard, Once};

const GOBBY_HOME_ENV: &str = "GOBBY_HOME";
const MANAGED_AGENT_ENV_KEYS: &[&str] = &[
    "GOBBY_AGENT_RUN_ID",
    "GOBBY_MANAGED_EXECUTION_ID",
    gobby_core::grant::MANAGED_BOOTSTRAP_ENV,
    "GOBBY_SESSION_ID",
    "GOBBY_PARENT_SESSION_ID",
    gobby_core::local_token::AGENT_API_TOKEN_ENV,
    "GOBBY_DAEMON_URL",
    "GOBBY_PORT",
    "GOBBY_DAEMON_PORT",
];

/// Serializes environment mutations made through `EnvGuard` in this crate's
/// tests.
///
/// This lock only protects callers that use the helper. It cannot make global
/// process environment access safe for tests that read or write env vars
/// directly while another guarded mutation is active.
pub(crate) static ENV_TEST_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

pub(crate) fn ensure_clean_effective_config() {
    static CLEAN_EFFECTIVE_CONFIG: Once = Once::new();

    CLEAN_EFFECTIVE_CONFIG.call_once(|| {
        let home = tempfile::tempdir().expect("test Gobby home");
        let (daemon_url, request) =
            crate::test_http::spawn_json_response(r#"{"revision":0,"config":{}}"#)
                .expect("spawn effective-config test server");
        let daemon_url = OsString::from(daemon_url);

        with_clean_env_at(home.path().as_os_str(), daemon_url.as_os_str(), || {
            gobby_core::ai::effective_config::daemon_mode_layers()
                .expect("load isolated effective config");
        });

        let request = request
            .join()
            .expect("join effective-config test server")
            .expect("read effective-config test request");
        assert!(request.starts_with("GET /api/config/effective HTTP/1.1"));
    });
}

pub(crate) fn with_postgres_test_env<R>(root: &Path, closure: impl FnOnce() -> R) -> R {
    ensure_clean_effective_config();
    const PROJECT_ID: &str = "11111111-1111-4111-8111-111111111111";
    const MACHINE_ID: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";

    let home = tempfile::tempdir().expect("test Gobby home");
    std::fs::write(home.path().join("machine_id"), MACHINE_ID).expect("write test machine id");
    std::fs::create_dir_all(root.join(".gobby")).expect("create test project metadata");
    std::fs::write(
        root.join(".gobby/project.json"),
        format!(r#"{{"id":"{PROJECT_ID}"}}"#),
    )
    .expect("write test project identity");
    let database_url = std::env::var("DATABASE_URL").expect("DATABASE_URL for PostgreSQL tests");
    let grant = gobby_core::grant::managed_direct_grant(
        PROJECT_ID,
        MACHINE_ID,
        &gobby_core::grant::DirectConnections::postgres(database_url),
    );
    let grant_path =
        gobby_core::grant::write_managed_bootstrap(&home.path().join("grants"), &grant)
            .expect("write managed test grant");

    crate::support::env::set_active_project_root(Some(root.to_path_buf()));
    let result = temp_env::with_vars(
        [
            (GOBBY_HOME_ENV, Some(home.path().as_os_str())),
            ("GOBBY_AGENT_RUN_ID", None),
            ("GOBBY_MANAGED_EXECUTION_ID", None),
            (
                gobby_core::grant::MANAGED_BOOTSTRAP_ENV,
                Some(grant_path.as_os_str()),
            ),
            ("GOBBY_SESSION_ID", None),
            ("GOBBY_PARENT_SESSION_ID", None),
            (gobby_core::local_token::AGENT_API_TOKEN_ENV, None),
            ("GOBBY_DAEMON_URL", Some(OsStr::new("http://127.0.0.1:9"))),
            ("GOBBY_PORT", None),
            ("GOBBY_DAEMON_PORT", None),
        ],
        closure,
    );
    crate::support::env::set_active_project_root(None);
    result
}

fn with_clean_env_at<R>(home: &OsStr, daemon_url: &OsStr, closure: impl FnOnce() -> R) -> R {
    temp_env::with_vars(
        [
            (GOBBY_HOME_ENV, Some(home)),
            ("GOBBY_AGENT_RUN_ID", None),
            ("GOBBY_MANAGED_EXECUTION_ID", None),
            (gobby_core::grant::MANAGED_BOOTSTRAP_ENV, None),
            ("GOBBY_SESSION_ID", None),
            ("GOBBY_PARENT_SESSION_ID", None),
            (gobby_core::local_token::AGENT_API_TOKEN_ENV, None),
            ("GOBBY_DAEMON_URL", Some(daemon_url)),
            ("GOBBY_PORT", None),
            ("GOBBY_DAEMON_PORT", None),
        ],
        closure,
    )
}

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

    pub(crate) fn and_set(mut self, key: &'static str, value: impl AsRef<OsStr>) -> Self {
        self.set_value(key, value.as_ref());
        self
    }

    fn locked() -> Self {
        // Recover from poison: a panicking guard still restores its variables
        // during unwind, so the environment stays consistent and one failing
        // test must not cascade into lock-acquisition failures everywhere else.
        let lock = ENV_TEST_LOCK
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        Self {
            old_values: Vec::new(),
            _lock: lock,
        }
    }

    fn set_value(&mut self, key: &'static str, value: &OsStr) {
        if key == GOBBY_HOME_ENV {
            self.scrub_managed_agent_env();
        }
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

    fn scrub_managed_agent_env(&mut self) {
        for key in MANAGED_AGENT_ENV_KEYS {
            self.unset_value(key);
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
