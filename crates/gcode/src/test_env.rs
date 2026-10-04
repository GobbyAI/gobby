const GCODE_POSTGRES_TEST_DATABASE_URL_ENV: &str = "GCODE_POSTGRES_TEST_DATABASE_URL";
#[cfg(test)]
const GOBBY_HOME_ENV: &str = "GOBBY_HOME";
const GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE_ENV: &str = "GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE";
// The fixture applies the schema and seeds users/machines rows, so it owns its
// database outright; pytest's gobby_test is a different `*_test` database.
const GCODE_POSTGRES_TEST_DATABASE: &str = "gobby_gcode_test";

pub fn postgres_test_database_url(purpose: &str) -> String {
    let database_url = resolve_postgres_test_database_url(purpose);
    ensure_code_index_schema(&database_url);
    database_url
}

/// Resolve the test database URL and refuse any database but
/// `gobby_gcode_test` unless `GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE` overrides
/// the guard. Only the gcode-specific variable is read: the pytest stack's
/// `DATABASE_URL` and `GOBBY_POSTGRES_TEST_*` point at gobby_test, and the
/// operator's bootstrap.yaml points at the live hub.
fn resolve_postgres_test_database_url(purpose: &str) -> String {
    let Some(database_url) = postgres_test_database_url_from_sources() else {
        panic!(
            "{purpose} requires a PostgreSQL test database URL; set \
             {GCODE_POSTGRES_TEST_DATABASE_URL_ENV} to the \
             `{GCODE_POSTGRES_TEST_DATABASE}` database"
        )
    };
    if let Err(reason) = destructive_postgres_test_allowed(&database_url) {
        panic!(
            "{purpose} refused the resolved PostgreSQL database URL: {reason}. \
             gcode DB tests only run against `{GCODE_POSTGRES_TEST_DATABASE}`; set \
             {GCODE_POSTGRES_TEST_DATABASE_URL_ENV} to it or set \
             {GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE_ENV}=1 to bypass this guard"
        );
    }
    database_url
}

fn postgres_test_database_url_from_sources() -> Option<String> {
    non_empty_env(GCODE_POSTGRES_TEST_DATABASE_URL_ENV)
}

fn non_empty_env(name: &str) -> Option<String> {
    std::env::var(name)
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

/// Refuse destructive operations against any database but
/// `gobby_gcode_test`, unless `GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE`
/// explicitly overrides the guard.
pub fn destructive_postgres_test_allowed(database_url: &str) -> Result<(), String> {
    if destructive_postgres_test_override_enabled() {
        return Ok(());
    }
    let config = database_url
        .parse::<postgres::Config>()
        .map_err(|error| format!("database URL could not be parsed: {error}"))?;
    match config.get_dbname() {
        Some(GCODE_POSTGRES_TEST_DATABASE) => Ok(()),
        Some(name) => Err(format!(
            "database name `{name}` is not the dedicated `{GCODE_POSTGRES_TEST_DATABASE}`"
        )),
        None => Err("database URL does not include a database name".to_string()),
    }
}

pub fn destructive_postgres_test_override_enabled() -> bool {
    std::env::var(GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE_ENV)
        .ok()
        .is_some_and(|value| value == "1" || value.eq_ignore_ascii_case("true"))
}

/// Provision the code-index schema once per process so DB-backed tests pass
/// from any starting database state via hub schema apply. Any database the
/// guard refuses is left untouched.
fn ensure_code_index_schema(database_url: &str) {
    static PROVISIONED: std::sync::OnceLock<
        std::sync::Mutex<std::collections::HashMap<String, Result<(), String>>>,
    > = std::sync::OnceLock::new();
    let provisioned =
        PROVISIONED.get_or_init(|| std::sync::Mutex::new(std::collections::HashMap::new()));
    let mut provisioned = provisioned
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let result = provisioned
        .entry(database_url.to_string())
        .or_insert_with(|| provision_code_index_schema(database_url));
    if let Err(message) = result {
        panic!("provisioning the code-index test schema failed: {message}");
    }
}

fn provision_code_index_schema(database_url: &str) -> Result<(), String> {
    if let Err(reason) = destructive_postgres_test_allowed(database_url) {
        eprintln!("skipping code-index test schema provisioning: {reason}");
        return Ok(());
    }
    let mut client = gobby_core::postgres::connect_readwrite(database_url)
        .map_err(|error| format!("connect to the test database: {error:#}"))?;
    {
        let mut runner = gobby_core::schema::SchemaRunner::new(&mut client, "public")
            .map_err(|error| format!("schema runner: {error:#}"))?;
        runner
            .apply()
            .map(|_| ())
            .map_err(|error| format!("gdaemon schema apply: {error:#}"))?;
    }
    let machine_id = gobby_core::machine::read_local_machine_id()
        .map_err(|error| format!("read local machine id: {error:#}"))?;
    seed_test_machine(&mut client, &machine_id)
}

/// Register `machine_id` in `machines` under a synthetic test owner.
///
/// Code-index selector tables reference `machines`, and a fresh test database
/// has no row for this machine or for the synthetic machines that
/// multi-machine tests invent. Idempotent, so tests can call it freely.
pub fn seed_test_machine(client: &mut postgres::Client, machine_id: &str) -> Result<(), String> {
    let machine_id = uuid::Uuid::parse_str(machine_id)
        .map_err(|error| format!("parse test machine id {machine_id:?}: {error}"))?;
    let owner_id: uuid::Uuid = client
        .query_one(
            "INSERT INTO users (id, email, name, password_hash)
             VALUES (gen_random_uuid(), $1, 'gcode tests', 'unused')
             ON CONFLICT (lower(email)) DO UPDATE SET updated_at = now()
             RETURNING id",
            &[&"gcode-tests@example.invalid"],
        )
        .map_err(|error| format!("seed test owner user: {error}"))?
        .get(0);
    client
        .execute(
            "INSERT INTO machines (id, hostname, owner_user_id)
             VALUES ($1, 'gcode-tests', $2)
             ON CONFLICT (id) DO NOTHING",
            &[&machine_id, &owner_id],
        )
        .map_err(|error| format!("seed local machine row: {error}"))?;
    Ok(())
}

/// Register `root` as this machine's checkout for `project_id`, creating the
/// registry project row when needed. Primary index writes are fenced on this
/// row, so every test that indexes a root in Primary mode registers it first.
/// Idempotent: a re-run with a fresh temp root rebinds the same project id.
pub fn seed_test_checkout(
    client: &mut postgres::Client,
    project_id: &str,
    root: &std::path::Path,
) -> Result<(), String> {
    let root = root
        .canonicalize()
        .map_err(|error| format!("canonicalize test checkout {}: {error}", root.display()))?;
    let machine_id = gobby_core::machine::read_local_machine_id()
        .map_err(|error| format!("read local machine id: {error:#}"))?;
    let machine_id = uuid::Uuid::parse_str(&machine_id)
        .map_err(|error| format!("parse local machine id {machine_id:?}: {error}"))?;
    let project_uuid = uuid::Uuid::parse_str(project_id)
        .map_err(|error| format!("parse test project id {project_id:?}: {error}"))?;
    client
        .execute(
            "INSERT INTO projects (id, name) VALUES ($1, $2)
             ON CONFLICT (id) DO NOTHING",
            &[&project_uuid, &format!("gcode-test-{project_id}")],
        )
        .map_err(|error| format!("seed registry project row: {error}"))?;
    client
        .execute(
            "INSERT INTO project_checkouts (machine_id, project_id, root_path)
             VALUES ($1, $2, $3)
             ON CONFLICT (machine_id, project_id) DO UPDATE SET
                root_path = EXCLUDED.root_path,
                updated_at = now()",
            &[&machine_id, &project_uuid, &root.to_string_lossy()],
        )
        .map_err(|error| format!("seed local checkout row: {error}"))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    const POSTGRES_TEST_ENV_KEYS: &[&str] = &[
        GCODE_POSTGRES_TEST_DATABASE_URL_ENV,
        "GOBBY_POSTGRES_TEST_DATABASE_URL",
        "DATABASE_URL",
        "GOBBY_POSTGRES_TEST_DB",
        "GOBBY_POSTGRES_TEST_USER",
        "GOBBY_POSTGRES_TEST_PASSWORD",
        "GOBBY_POSTGRES_TEST_HOST",
        "GOBBY_POSTGRES_TEST_PORT",
        "GOBBY_AGENT_RUN_ID",
        "GOBBY_MANAGED_EXECUTION_ID",
        gobby_core::grant::MANAGED_BOOTSTRAP_ENV,
        "GOBBY_SESSION_ID",
        "GOBBY_PARENT_SESSION_ID",
        gobby_core::local_token::AGENT_API_TOKEN_ENV,
        "GOBBY_DAEMON_URL",
        "GOBBY_PORT",
        "GOBBY_DAEMON_PORT",
        GOBBY_HOME_ENV,
        GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE_ENV,
    ];

    fn with_postgres_test_env<R>(
        overrides: &[(&str, Option<&str>)],
        closure: impl FnOnce() -> R,
    ) -> R {
        let vars = POSTGRES_TEST_ENV_KEYS
            .iter()
            .map(|key| {
                let value = overrides
                    .iter()
                    .find_map(|(name, value)| (*name == *key).then_some(*value))
                    .unwrap_or(None);
                (*key, value)
            })
            .collect::<Vec<_>>();
        temp_env::with_vars(vars, closure)
    }

    #[test]
    #[serial_test::serial(serial_db)]
    fn test_env_reads_the_gcode_specific_database_url() {
        with_postgres_test_env(
            &[(
                GCODE_POSTGRES_TEST_DATABASE_URL_ENV,
                Some("postgresql://localhost/gobby_gcode_test"),
            )],
            || {
                assert_eq!(
                    postgres_test_database_url_from_sources().as_deref(),
                    Some("postgresql://localhost/gobby_gcode_test")
                );
            },
        );
    }

    // DATABASE_URL and the GOBBY_POSTGRES_TEST_* family are the pytest stack's
    // and point at gobby_test; the gcode fixture must never pick them up.
    #[test]
    #[serial_test::serial(serial_db)]
    fn test_env_ignores_pytest_database_sources() {
        with_postgres_test_env(
            &[
                (
                    "GOBBY_POSTGRES_TEST_DATABASE_URL",
                    Some("postgresql://localhost/gobby_test"),
                ),
                ("DATABASE_URL", Some("postgresql://localhost/gobby_test")),
                ("GOBBY_POSTGRES_TEST_DB", Some("gobby_test")),
                ("GOBBY_POSTGRES_TEST_USER", Some("gobby_test")),
            ],
            || {
                assert_eq!(postgres_test_database_url_from_sources().as_deref(), None);
            },
        );
    }

    #[test]
    #[serial_test::serial(serial_db)]
    fn test_env_does_not_use_bootstrap_yaml() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("bootstrap.yaml"),
            "database_url: postgresql://bootstrap/gobby\n",
        )
        .unwrap();
        let home = dir.path().to_str().unwrap();

        with_postgres_test_env(&[(GOBBY_HOME_ENV, Some(home))], || {
            assert_eq!(postgres_test_database_url_from_sources().as_deref(), None);
        });
    }

    #[test]
    #[serial_test::serial(serial_db)]
    fn resolve_refuses_non_test_database_without_override() {
        with_postgres_test_env(
            &[(
                GCODE_POSTGRES_TEST_DATABASE_URL_ENV,
                Some("postgresql://localhost/gobby_test"),
            )],
            || {
                let panic =
                    std::panic::catch_unwind(|| resolve_postgres_test_database_url("guard tests"))
                        .expect_err("pytest's database URL is refused");
                let message = panic
                    .downcast_ref::<String>()
                    .expect("panic payload is a formatted String");
                assert!(
                    message.contains("`gobby_test` is not the dedicated `gobby_gcode_test`"),
                    "{message}"
                );
                assert!(
                    message.contains(GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE_ENV),
                    "{message}"
                );
            },
        );
    }

    #[test]
    #[serial_test::serial(serial_db)]
    fn resolve_allows_non_test_database_with_override() {
        with_postgres_test_env(
            &[
                (
                    GCODE_POSTGRES_TEST_DATABASE_URL_ENV,
                    Some("postgresql://localhost/gobby"),
                ),
                (GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE_ENV, Some("1")),
            ],
            || {
                assert_eq!(
                    resolve_postgres_test_database_url("guard tests"),
                    "postgresql://localhost/gobby"
                );
            },
        );
    }

    #[test]
    #[serial_test::serial(serial_db)]
    fn resolve_passes_explicit_test_database_unchanged() {
        with_postgres_test_env(
            &[(
                GCODE_POSTGRES_TEST_DATABASE_URL_ENV,
                Some("postgresql://localhost/gobby_gcode_test"),
            )],
            || {
                assert_eq!(
                    resolve_postgres_test_database_url("guard tests"),
                    "postgresql://localhost/gobby_gcode_test"
                );
            },
        );
    }

    #[test]
    #[serial_test::serial(serial_db)]
    fn destructive_postgres_guard_accepts_only_the_dedicated_database() {
        temp_env::with_var(
            GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE_ENV,
            Option::<&str>::None,
            || {
                assert!(
                    destructive_postgres_test_allowed("postgresql://localhost/gobby_gcode_test")
                        .is_ok()
                );
                for name in ["gobby_test", "gcode_test", "gobby"] {
                    let error = destructive_postgres_test_allowed(&format!(
                        "postgresql://localhost/{name}"
                    ))
                    .expect_err("only gobby_gcode_test is accepted");
                    assert!(
                        error
                            .contains(&format!("`{name}` is not the dedicated `gobby_gcode_test`")),
                        "{error}"
                    );
                }
            },
        );
    }

    #[test]
    #[serial_test::serial(serial_db)]
    fn destructive_postgres_guard_accepts_explicit_override_values() {
        for value in ["1", "true", "TRUE"] {
            temp_env::with_var(
                GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE_ENV,
                Some(value),
                || {
                    assert!(
                        destructive_postgres_test_allowed("postgresql://localhost/gcode").is_ok()
                    );
                },
            );
        }
        temp_env::with_var(GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE_ENV, Some("0"), || {
            assert!(destructive_postgres_test_allowed("postgresql://localhost/gcode").is_err());
        });
    }
}

// All library tests share log's single global logger. Capture per thread so
// concurrently running tests cannot clear or assert on each other's records.
#[cfg(test)]
mod logs {
    use std::cell::RefCell;

    thread_local! {
        static RECORDS: RefCell<Vec<(log::Level, String)>> = const { RefCell::new(Vec::new()) };
    }

    struct CaptureLogger;

    impl log::Log for CaptureLogger {
        fn enabled(&self, metadata: &log::Metadata<'_>) -> bool {
            metadata.level() <= log::Level::Debug
        }

        fn log(&self, record: &log::Record<'_>) {
            if self.enabled(record.metadata()) {
                let _ = RECORDS.try_with(|records| {
                    records
                        .borrow_mut()
                        .push((record.level(), record.args().to_string()));
                });
            }
        }

        fn flush(&self) {}
    }

    pub(super) fn clear() {
        static INSTALL: std::sync::Once = std::sync::Once::new();
        INSTALL.call_once(|| {
            static LOGGER: CaptureLogger = CaptureLogger;
            log::set_logger(&LOGGER).expect("install shared library-test logger");
            log::set_max_level(log::LevelFilter::Debug);
        });
        RECORDS.with(|records| records.borrow_mut().clear());
    }

    pub(super) fn captured(level: log::Level) -> Vec<String> {
        RECORDS.with(|records| {
            records
                .borrow()
                .iter()
                .filter(|(record_level, _)| *record_level <= level)
                .map(|(_, message)| message.clone())
                .collect()
        })
    }
}

#[cfg(test)]
pub(crate) fn clear_captured_logs() {
    logs::clear();
}

#[cfg(test)]
pub(crate) fn captured_logs(level: log::Level) -> Vec<String> {
    logs::captured(level)
}
