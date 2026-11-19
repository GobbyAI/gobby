fn main() {
    println!("cargo:rerun-if-env-changed=DATABASE_URL");
    println!("cargo:rustc-check-cfg=cfg(gcode_postgres_tests)");

    if has_postgres_test_database() {
        println!("cargo:rustc-cfg=gcode_postgres_tests");
    }
}

fn has_postgres_test_database() -> bool {
    // Must match crates/gcode/src/test_env.rs, which reads DATABASE_URL through the
    // shared guard entry. The cfg only selects tests inside test builds; the module
    // itself is gated on `test` or the feature named in H37.
    std::env::var_os("DATABASE_URL")
        .is_some_and(|value| value.to_str().is_some_and(|text| !text.trim().is_empty()))
}
