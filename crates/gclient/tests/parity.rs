//! 4.1: herdr keep-set render-test parity suite.
//!
//! The harness lives in `parity/mod.rs`; this file only roots the integration
//! test binary so nextest lists every ported test as `parity::<module>::<name>`.

#[path = "parity/mod.rs"]
mod parity;
