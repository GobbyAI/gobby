fn main() {
    println!("cargo:rerun-if-env-changed=GCODE_POSTGRES_TEST_DATABASE_URL");
    println!("cargo:rustc-check-cfg=cfg(gcode_postgres_tests)");

    if std::env::var_os("GCODE_POSTGRES_TEST_DATABASE_URL").is_some() {
        println!("cargo:rustc-cfg=gcode_postgres_tests");
    }
}
