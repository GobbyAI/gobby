//! Integration suite for `gobby-port`.
//!
//! Each verb's tests are one module of this program and run the built `gport`.

#[allow(dead_code)]
mod support;

#[path = "census.rs"]
mod census;

#[path = "ledger.rs"]
mod ledger;

#[path = "record.rs"]
mod record;

#[path = "shapes.rs"]
mod shapes;

#[path = "slice.rs"]
mod slice;

#[path = "start.rs"]
mod start;
