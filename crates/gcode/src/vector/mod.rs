pub mod code_symbols;

#[cfg(all(test, feature = "ai"))]
#[path = "tests/projection.rs"]
mod projection_tests;
