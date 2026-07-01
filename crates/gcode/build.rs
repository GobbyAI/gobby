fn main() {
    for name in [
        "GCODE_POSTGRES_TEST_DATABASE_URL",
        "GOBBY_POSTGRES_TEST_DATABASE_URL",
        "DATABASE_URL",
        "GOBBY_POSTGRES_TEST_DB",
        "GOBBY_POSTGRES_TEST_USER",
        "GOBBY_POSTGRES_TEST_PASSWORD",
        "GOBBY_POSTGRES_TEST_HOST",
        "GOBBY_POSTGRES_TEST_PORT",
        "GOBBY_HOME",
    ] {
        println!("cargo:rerun-if-env-changed={name}");
    }
    if let Some(path) = gobby_core::bootstrap::bootstrap_path() {
        println!("cargo:rerun-if-changed={}", path.display());
    }
    println!("cargo:rustc-check-cfg=cfg(gcode_postgres_tests)");

    if has_postgres_test_database() {
        println!("cargo:rustc-cfg=gcode_postgres_tests");
    }
}

fn has_postgres_test_database() -> bool {
    [
        "GCODE_POSTGRES_TEST_DATABASE_URL",
        "GOBBY_POSTGRES_TEST_DATABASE_URL",
        "DATABASE_URL",
    ]
    .iter()
    .any(|name| non_empty_env(name))
        || ["GOBBY_POSTGRES_TEST_DB", "GOBBY_POSTGRES_TEST_USER"]
            .iter()
            .all(|name| non_empty_env(name))
        || has_bootstrap_postgres_database_url()
}

fn has_bootstrap_postgres_database_url() -> bool {
    let Some(path) = gobby_core::bootstrap::bootstrap_path() else {
        return false;
    };
    match gobby_core::bootstrap::postgres_database_url_from_bootstrap_file(&path) {
        Ok(Some(_)) => true,
        Ok(None) => false,
        Err(error) => {
            println!(
                "cargo:warning=failed to read PostgreSQL test database URL from {}: {error:#}",
                path.display()
            );
            false
        }
    }
}

fn non_empty_env(name: &str) -> bool {
    std::env::var(name).is_ok_and(|value| !value.trim().is_empty())
}
