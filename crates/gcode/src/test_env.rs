const GCODE_POSTGRES_TEST_DATABASE_URL_ENV: &str = "GCODE_POSTGRES_TEST_DATABASE_URL";
const GOBBY_POSTGRES_TEST_DATABASE_URL_ENV: &str = "GOBBY_POSTGRES_TEST_DATABASE_URL";
const DATABASE_URL_ENV: &str = "DATABASE_URL";

pub(crate) fn postgres_test_database_url(purpose: &str) -> String {
    postgres_test_database_url_from_env().unwrap_or_else(|| {
        panic!(
            "{purpose} requires a PostgreSQL test database URL; set \
             {GCODE_POSTGRES_TEST_DATABASE_URL_ENV}, \
             {GOBBY_POSTGRES_TEST_DATABASE_URL_ENV}, {DATABASE_URL_ENV}, \
             or GOBBY_POSTGRES_TEST_* components"
        )
    })
}

fn postgres_test_database_url_from_env() -> Option<String> {
    [
        GCODE_POSTGRES_TEST_DATABASE_URL_ENV,
        GOBBY_POSTGRES_TEST_DATABASE_URL_ENV,
        DATABASE_URL_ENV,
    ]
    .iter()
    .find_map(|name| non_empty_env(name))
    .or_else(postgres_test_database_url_from_parts)
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

fn non_empty_env(name: &str) -> Option<String> {
    std::env::var(name)
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}
