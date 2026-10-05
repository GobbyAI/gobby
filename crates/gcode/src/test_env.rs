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
/// the `gobby_gcode_test` namespace unless `GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE` overrides
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
    let config = database_url
        .parse::<postgres::Config>()
        .expect("validated test DSN");
    if config.get_dbname().is_some_and(is_gcode_test_database) {
        let identity = gobby_core::schema::schema_identity();
        database_url_for_name(
            &database_url,
            &schema_test_database_name(identity.latest_asset.version, &identity.root_hash),
        )
    } else {
        database_url
    }
}

// A 128-bit hash prefix keeps cohort names below PostgreSQL's 63-byte limit.
fn schema_test_database_name(version: i32, root_hash: &str) -> String {
    format!(
        "{GCODE_POSTGRES_TEST_DATABASE}_v{version}_{}",
        &root_hash[..32]
    )
}

fn is_gcode_test_database(name: &str) -> bool {
    name == GCODE_POSTGRES_TEST_DATABASE
        || name.starts_with(&format!("{GCODE_POSTGRES_TEST_DATABASE}_"))
}

fn database_url_for_name(database_url: &str, name: &str) -> String {
    if let Ok(mut url) = reqwest::Url::parse(database_url) {
        url.set_path(&format!("/{name}"));
        // PostgreSQL URI queries treat '+' literally, unlike form encoding.
        if let Some(query) = url.query().map(|query| query.replace('+', "%2B")) {
            url.set_query(Some(&query));
        }
        let options: Vec<_> = url
            .query_pairs()
            .filter(|(key, _)| key != "dbname")
            .map(|(key, value)| (key.into_owned(), value.into_owned()))
            .collect();
        if url.query().is_some() {
            url.query_pairs_mut().clear().extend_pairs(options);
            let query = url
                .query()
                .expect("preserved DSN query")
                .replace('+', "%20");
            url.set_query(Some(&query));
        }
        url.to_string()
    } else {
        // libpq keyword strings use the last value for a repeated option.
        format!("{database_url} dbname='{}'", name.replace('\'', "\\'"))
    }
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
        Some(name) if is_gcode_test_database(name) => Ok(()),
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
    let _provisioning_lock = ensure_test_database_exists(database_url)?;
    let mut client = gobby_core::postgres::connect_readwrite(database_url)
        .map_err(|error| format!("connect to the test database: {error:#}"))?;
    client
        .batch_execute("CREATE EXTENSION IF NOT EXISTS pg_search")
        .map_err(|error| format!("install test pg_search extension: {error:#}"))?;
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

/// Keep this connection alive to serialize database, extension and schema setup.
fn ensure_test_database_exists(database_url: &str) -> Result<postgres::Client, String> {
    let config = database_url
        .parse::<postgres::Config>()
        .map_err(|error| error.to_string())?;
    let name = config.get_dbname().ok_or("test database name is missing")?;
    let admin_url = database_url_for_name(database_url, "postgres");
    let mut admin = gobby_core::postgres::connect_readwrite(&admin_url)
        .map_err(|error| format!("connect to test hub for provisioning: {error:#}"))?;
    admin
        .execute("SELECT pg_advisory_lock(hashtext($1))", &[&name])
        .map_err(|error| format!("lock test database creation: {error:#}"))?;
    let exists = admin
        .query_opt("SELECT 1 FROM pg_database WHERE datname = $1", &[&name])
        .map_err(|error| format!("inspect test database: {error:#}"))?
        .is_some();
    if !exists {
        admin
            .batch_execute(&format!(
                "CREATE DATABASE \"{}\"",
                name.replace('"', "\"\"")
            ))
            .map_err(|error| format!("create schema-versioned test database: {error:#}"))?;
    }
    // The connection owns the advisory lock, including on error paths.
    Ok(admin)
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
    fn resolve_selects_compiled_schema_database() {
        with_postgres_test_env(
            &[(
                GCODE_POSTGRES_TEST_DATABASE_URL_ENV,
                Some("postgresql://localhost/gobby_gcode_test"),
            )],
            || {
                assert_eq!(resolve_postgres_test_database_url("guard tests"), {
                    let identity = gobby_core::schema::schema_identity();
                    format!(
                        "postgresql://localhost/{}",
                        schema_test_database_name(
                            identity.latest_asset.version,
                            &identity.root_hash
                        )
                    )
                });
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
                for name in [
                    "gobby_gcode_test",
                    "gobby_gcode_test_v458",
                    "gobby_gcode_test_v459",
                ] {
                    assert!(
                        destructive_postgres_test_allowed(&format!(
                            "postgresql://localhost/{name}"
                        ))
                        .is_ok(),
                        "{name}"
                    );
                }
                for name in [
                    "gobby_test",
                    "gcode_test",
                    "gobby",
                    "gobby_gcode_testing",
                    "gobby_gcode_testevil",
                ] {
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
    fn schema_database_selection_preserves_options_and_ignores_newer_target() {
        let identity = gobby_core::schema::schema_identity();
        let version = identity.latest_asset.version;
        let name = schema_test_database_name(version, &identity.root_hash);
        for source in [
            format!("postgresql://fixture:secret@127.0.0.1:60892/gobby_gcode_test_v{}?application_name=fixture+app&sslmode=disable&options=-c%20statement_timeout%3D1000", version + 1),
            "host=127.0.0.1 port=60892 user=fixture password=secret dbname=gobby_gcode_test application_name=fixture+app sslmode=disable options='-c statement_timeout=1000'".to_string(),
        ] {
            with_postgres_test_env(&[(GCODE_POSTGRES_TEST_DATABASE_URL_ENV, Some(&source))], || {
                let first = resolve_postgres_test_database_url("schema cohorts");
                let second = resolve_postgres_test_database_url("schema cohorts");
                assert_eq!(first, second, "same compiled schema shares a target");
                let config = first.parse::<postgres::Config>().expect("selected DSN");
                assert_eq!(config.get_dbname(), Some(name.as_str()));
                assert_eq!(config.get_user(), Some("fixture"));
                assert_eq!(config.get_password(), Some(b"secret".as_slice()));
                assert_eq!(config.get_ports(), &[60892]);
                assert_eq!(config.get_application_name(), Some("fixture+app"));
                assert_eq!(config.get_options(), Some("-c statement_timeout=1000"));
                assert_eq!(config.get_ssl_mode(), postgres::config::SslMode::Disable);
            });
        }
    }

    #[test]
    fn schema_database_cohorts_separate_divergent_roots_at_the_same_version() {
        let identity = gobby_core::schema::schema_identity();
        let first = schema_test_database_name(identity.latest_asset.version, &identity.root_hash);
        let mut other_root = identity.root_hash.clone();
        other_root.replace_range(
            ..1,
            if other_root.starts_with('0') {
                "1"
            } else {
                "0"
            },
        );
        let second = schema_test_database_name(identity.latest_asset.version, &other_root);
        assert_ne!(
            first, second,
            "divergent migration roots must not share a database"
        );
        assert_eq!(
            first,
            schema_test_database_name(identity.latest_asset.version, &identity.root_hash)
        );
        assert!(
            first.len() <= 63,
            "PostgreSQL must not truncate the cohort name"
        );
        assert!(is_gcode_test_database(&first));
        assert!(is_gcode_test_database(&second));
    }

    #[test]
    #[cfg(gcode_postgres_tests)]
    #[serial_test::serial(serial_db)]
    fn serial_db_newer_schema_database_does_not_poison_compiled_schema_cohort() -> anyhow::Result<()>
    {
        let identity = gobby_core::schema::schema_identity();
        let version = identity.latest_asset.version;
        let source = postgres_test_database_url_from_sources().expect("DB test DSN");
        // Only this unique disposable probe database carries the simulated
        // future receipt. Never alter or drop another runner's shared cohort.
        let future_name = format!(
            "gobby_gcode_test_v{}_probe_{}",
            version + 1,
            &uuid::Uuid::new_v4().simple().to_string()[..8]
        );
        let future_url = database_url_for_name(&source, &future_name);
        struct ProbeDatabase {
            admin: postgres::Client,
            name: String,
        }
        impl Drop for ProbeDatabase {
            fn drop(&mut self) {
                if let Err(error) = self
                    .admin
                    .batch_execute(&format!("DROP DATABASE \"{}\"", self.name))
                {
                    if std::thread::panicking() {
                        eprintln!("probe database cleanup failed: {error}");
                    } else {
                        panic!("probe database cleanup failed: {error}");
                    }
                }
            }
        }
        let _probe = ProbeDatabase {
            admin: ensure_test_database_exists(&future_url).map_err(anyhow::Error::msg)?,
            name: future_name,
        };
        (|| -> anyhow::Result<()> {
            let mut future = gobby_core::postgres::connect_readwrite(&future_url)?;
            future.batch_execute("CREATE EXTENSION IF NOT EXISTS pg_search; CREATE TABLE schema_migrations (version integer PRIMARY KEY)")?;
            future.execute(
                "INSERT INTO schema_migrations(version) VALUES ($1)",
                &[&(version + 1)],
            )?;
            let error = gobby_core::schema::SchemaRunner::new(&mut future, "public")?
                .apply()
                .expect_err("future schema refuses this older runner");
            assert!(
                error.to_string().contains("newer than this runner"),
                "{error}"
            );
            temp_env::with_var(
                GCODE_POSTGRES_TEST_DATABASE_URL_ENV,
                Some(future_url.as_str()),
                || {
                    let selected = postgres_test_database_url("schema cohort regression");
                    assert_eq!(
                        selected
                            .parse::<postgres::Config>()
                            .expect("cohort DSN")
                            .get_dbname(),
                        Some(schema_test_database_name(version, &identity.root_hash).as_str())
                    );
                    assert_eq!(postgres_test_database_url("same process"), selected);
                    let mut current = gobby_core::postgres::connect_readwrite(&selected)
                        .expect("compiled cohort connection");
                    gobby_core::schema::SchemaRunner::new(&mut current, "public")
                        .expect("compiled runner")
                        .apply()
                        .expect("compiled schema remains applicable");
                },
            );
            Ok(())
        })()
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
