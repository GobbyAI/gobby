const GCODE_POSTGRES_TEST_DATABASE_URL_ENV: &str = "GCODE_POSTGRES_TEST_DATABASE_URL";
const GOBBY_POSTGRES_TEST_DATABASE_URL_ENV: &str = "GOBBY_POSTGRES_TEST_DATABASE_URL";
const DATABASE_URL_ENV: &str = "DATABASE_URL";
const GOBBY_HOME_ENV: &str = "GOBBY_HOME";

pub fn postgres_test_database_url(purpose: &str) -> String {
    match postgres_test_database_url_from_sources() {
        Ok(Some(database_url)) => database_url,
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
    #[serial_test::serial]
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
    #[serial_test::serial]
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
    #[serial_test::serial]
    fn test_env_uses_bootstrap_fallback() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("bootstrap.yaml"),
            "hub_backend: postgres\ndatabase_url: postgresql://bootstrap/gobby\n",
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
}
