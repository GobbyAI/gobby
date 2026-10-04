fn main() {
    println!("cargo:rerun-if-env-changed=GCODE_POSTGRES_TEST_DATABASE_URL");
    println!("cargo:rustc-check-cfg=cfg(gcode_postgres_tests)");

    if has_postgres_test_database() {
        println!("cargo:rustc-cfg=gcode_postgres_tests");
    }
}

fn has_postgres_test_database() -> bool {
    // Must match crates/gcode/src/test_env.rs, which reads only this variable:
    // the pytest stack's DATABASE_URL and GOBBY_POSTGRES_TEST_* point at
    // gobby_test, and enabling this cfg from them compiles serial_db tests that
    // then panic at runtime when the resolver finds no gcode DSN.
    std::env::var_os("GCODE_POSTGRES_TEST_DATABASE_URL")
        .is_some_and(|value| value.to_str().is_some_and(|text| !text.trim().is_empty()))
}
