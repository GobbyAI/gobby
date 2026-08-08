const GCODE_POSTGRES_TEST_DATABASE_URL_ENV: &str = "GCODE_POSTGRES_TEST_DATABASE_URL";
const GOBBY_POSTGRES_TEST_DATABASE_URL_ENV: &str = "GOBBY_POSTGRES_TEST_DATABASE_URL";
const DATABASE_URL_ENV: &str = "DATABASE_URL";
const GOBBY_HOME_ENV: &str = "GOBBY_HOME";
const GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE_ENV: &str = "GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE";

pub fn postgres_test_database_url(purpose: &str) -> String {
    match postgres_test_database_url_from_sources() {
        Ok(Some(database_url)) => {
            ensure_code_index_schema(&database_url);
            database_url
        }
        Ok(None) => {
            panic!(
                "{purpose} requires a PostgreSQL test database URL; set \
                 {GCODE_POSTGRES_TEST_DATABASE_URL_ENV}, \
                 {GOBBY_POSTGRES_TEST_DATABASE_URL_ENV}, {DATABASE_URL_ENV}, \
                 GOBBY_POSTGRES_TEST_* components, or {GOBBY_HOME_ENV}/bootstrap.yaml"
            )
        }
        Err(error) => {
            panic!("{purpose} failed to read PostgreSQL test database URL sources: {error:#}")
        }
    }
}

fn postgres_test_database_url_from_sources() -> anyhow::Result<Option<String>> {
    if let Some(database_url) = [
        GCODE_POSTGRES_TEST_DATABASE_URL_ENV,
        GOBBY_POSTGRES_TEST_DATABASE_URL_ENV,
        DATABASE_URL_ENV,
    ]
    .iter()
    .find_map(|name| non_empty_env(name))
    {
        return Ok(Some(database_url));
    }

    if let Some(database_url) = postgres_test_database_url_from_parts() {
        return Ok(Some(database_url));
    }

    postgres_test_database_url_from_bootstrap()
}

fn postgres_test_database_url_from_parts() -> Option<String> {
    let database = non_empty_env("GOBBY_POSTGRES_TEST_DB")?;
    let user = non_empty_env("GOBBY_POSTGRES_TEST_USER")?;
    let password = non_empty_env("GOBBY_POSTGRES_TEST_PASSWORD").unwrap_or_default();
    let host = non_empty_env("GOBBY_POSTGRES_TEST_HOST").unwrap_or_else(|| "localhost".to_string());
    let port = non_empty_env("GOBBY_POSTGRES_TEST_PORT").unwrap_or_else(|| "5432".to_string());

    Some(format!(
        "postgresql://{user}:{password}@{host}:{port}/{database}"
    ))
}

fn postgres_test_database_url_from_bootstrap() -> anyhow::Result<Option<String>> {
    let Some(path) = gobby_core::bootstrap::bootstrap_path() else {
        return Ok(None);
    };
    gobby_core::bootstrap::postgres_database_url_from_bootstrap_file(&path)
}

fn non_empty_env(name: &str) -> Option<String> {
    std::env::var(name)
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

/// Refuse destructive operations against databases that are not clearly
/// disposable test databases (name ending in `_test`), unless
/// `GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE` explicitly overrides the guard.
pub fn destructive_postgres_test_allowed(database_url: &str) -> Result<(), String> {
    if destructive_postgres_test_override_enabled() {
        return Ok(());
    }
    let config = database_url
        .parse::<postgres::Config>()
        .map_err(|error| format!("database URL could not be parsed: {error}"))?;
    match config.get_dbname() {
        Some(name) if name.ends_with("_test") => Ok(()),
        Some(name) => Err(format!("database name `{name}` does not end with `_test`")),
        None => Err("database URL does not include a database name".to_string()),
    }
}

pub fn destructive_postgres_test_override_enabled() -> bool {
    std::env::var(GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE_ENV)
        .ok()
        .is_some_and(|value| value == "1" || value.eq_ignore_ascii_case("true"))
}

/// Provision the code-index schema once per process so DB-backed tests pass
/// from any starting database state without a manual `gcode setup --standalone`
/// between runs. Non-`_test` databases are left untouched.
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
    let mut request =
        crate::setup::StandaloneSetupRequest::new(true, Some(database_url.to_string()), None);
    request.overwrite_code_index = true;
    let status = crate::setup::run_standalone_setup(&request, &mut client)
        .map_err(|error| format!("standalone setup: {error}"))?;
    if status.failed.is_empty() {
        return Ok(());
    }
    Err(status
        .failed
        .iter()
        .map(|failure| format!("{}: {}", failure.name, failure.reason))
        .collect::<Vec<_>>()
        .join("; "))
}

#[cfg(test)]
mod tests {
    use super::*;

    const POSTGRES_TEST_ENV_KEYS: &[&str] = &[
        GCODE_POSTGRES_TEST_DATABASE_URL_ENV,
        GOBBY_POSTGRES_TEST_DATABASE_URL_ENV,
        DATABASE_URL_ENV,
        "GOBBY_POSTGRES_TEST_DB",
        "GOBBY_POSTGRES_TEST_USER",
        "GOBBY_POSTGRES_TEST_PASSWORD",
        "GOBBY_POSTGRES_TEST_HOST",
        "GOBBY_POSTGRES_TEST_PORT",
        GOBBY_HOME_ENV,
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
    fn test_env_prefers_gcode_specific_database_url() {
        with_postgres_test_env(
            &[
                (
                    GCODE_POSTGRES_TEST_DATABASE_URL_ENV,
                    Some("postgresql://gcode/db"),
                ),
                (
                    GOBBY_POSTGRES_TEST_DATABASE_URL_ENV,
                    Some("postgresql://gobby/db"),
                ),
                (DATABASE_URL_ENV, Some("postgresql://database/db")),
            ],
            || {
                assert_eq!(
                    postgres_test_database_url_from_sources()
                        .unwrap()
                        .as_deref(),
                    Some("postgresql://gcode/db")
                );
            },
        );
    }

    #[test]
    #[serial_test::serial(serial_db)]
    fn test_env_uses_component_var_fallback() {
        with_postgres_test_env(
            &[
                ("GOBBY_POSTGRES_TEST_DB", Some("gcode_test")),
                ("GOBBY_POSTGRES_TEST_USER", Some("tester")),
                ("GOBBY_POSTGRES_TEST_PASSWORD", Some("secret")),
                ("GOBBY_POSTGRES_TEST_HOST", Some("db.local")),
                ("GOBBY_POSTGRES_TEST_PORT", Some("15432")),
            ],
            || {
                assert_eq!(
                    postgres_test_database_url_from_sources()
                        .unwrap()
                        .as_deref(),
                    Some("postgresql://tester:secret@db.local:15432/gcode_test")
                );
            },
        );
    }

    #[test]
    #[serial_test::serial(serial_db)]
    fn test_env_uses_bootstrap_fallback() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("bootstrap.yaml"),
            "database_url: postgresql://bootstrap/gobby\n",
        )
        .unwrap();
        let home = dir.path().to_str().unwrap();

        with_postgres_test_env(&[(GOBBY_HOME_ENV, Some(home))], || {
            assert_eq!(
                postgres_test_database_url_from_sources()
                    .unwrap()
                    .as_deref(),
                Some("postgresql://bootstrap/gobby")
            );
        });
    }

    #[test]
    #[serial_test::serial(serial_db)]
    fn destructive_postgres_guard_requires_test_database_name() {
        temp_env::with_var(
            GCODE_POSTGRES_TEST_ALLOW_DESTRUCTIVE_ENV,
            Option::<&str>::None,
            || {
                assert!(
                    destructive_postgres_test_allowed("postgresql://localhost/gcode_test").is_ok()
                );
                let error = destructive_postgres_test_allowed("postgresql://localhost/gcode")
                    .expect_err("non-test database is rejected");
                assert!(error.contains("does not end with `_test`"));
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
