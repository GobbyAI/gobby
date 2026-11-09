//! Test-only fault seams. `GTERM_TEST_HELPER=1` arms the injected faults only in builds
//! with the `test-seams` feature. Release and default builds compile the probe to `false`,
//! so the variable is inert there, including in spawned children.

/// Whether this build honors `GTERM_TEST_HELPER=1`.
#[cfg(feature = "test-seams")]
pub(crate) fn helper_enabled() -> bool {
    std::env::var_os("GTERM_TEST_HELPER").is_some_and(|value| value == "1")
}

/// Whether this build honors `GTERM_TEST_HELPER=1`; without `test-seams` it never does.
#[cfg(not(feature = "test-seams"))]
pub(crate) const fn helper_enabled() -> bool {
    false
}

#[cfg(all(test, not(feature = "test-seams")))]
mod tests {
    use super::helper_enabled;

    #[test]
    fn helper_variable_is_inert_without_the_seam_feature() {
        // Edition 2021 keeps `set_var` safe; nextest runs each test in its own process.
        std::env::set_var("GTERM_TEST_HELPER", "1");
        let enabled = helper_enabled();
        std::env::remove_var("GTERM_TEST_HELPER");
        assert!(
            !enabled,
            "GTERM_TEST_HELPER must be inert without test-seams"
        );
    }
}
