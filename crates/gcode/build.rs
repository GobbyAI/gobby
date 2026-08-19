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
    ] {
        println!("cargo:rerun-if-env-changed={name}");
    }
    println!("cargo:rustc-check-cfg=cfg(gcode_postgres_tests)");

    if has_postgres_test_database() {
        println!("cargo:rustc-cfg=gcode_postgres_tests");
    }
}

fn has_postgres_test_database() -> bool {
    // Must match crates/gcode/src/test_env.rs: operator bootstrap.yaml is not a
    // test DSN. Enabling this cfg from bootstrap compiles serial_db tests and
    // then panics at runtime when the env resolver refuses that file.
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
}

fn non_empty_env(name: &str) -> bool {
    std::env::var_os(name).is_some_and(|value| !value.is_empty())
}
