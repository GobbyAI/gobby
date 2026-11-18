//! Development-only checks and records for the Gobby 1.0 port.
//!
//! `gport` is never promoted. Run it as
//! `cargo run --locked -q -p gobby-port -- <verb>`.
//!
//! Every verb's `run` writes its report to `out` and returns `Ok(true)` when
//! the check passes and `Ok(false)` when it refuses. An `Err` is a usage or
//! input error. `main` maps these to exit codes 0, 1 and 2.

pub mod census;
pub mod git;
pub mod ledger;
pub mod map;
pub mod record;
pub mod rows;
pub mod shapes;
pub mod slice;
pub mod start;

/// The epic whose direct children are the chunk tasks.
pub const PORT_EPIC: &str = "#21542";

/// The integration branch every chunk lands on.
pub const INTEGRATION_BRANCH: &str = "0.9.0";

pub const MAP_PATH: &str = "docs/port/map-v2.json";
pub const FROZEN_LEDGER_PATH: &str = "docs/port/ledger.md";
pub const RECORD_PATH: &str = "docs/port/record.md";
pub const RULINGS_PATH: &str = "docs/port/rulings.md";
pub const OPEN_NODES_PATH: &str = "docs/port/open-nodes.json";
pub const DISPOSITIONS_DIR: &str = "docs/port/dispositions";
pub const STATUS_DIR: &str = "docs/port/status";
pub const CONTRACT_LEDGER_PATH: &str = "docs/contracts/parity-ledger.md";
pub const STATUS_SHARDS_DIR: &str = "docs/contracts/parity-ledger-status.d";
pub const WAIVERS_PATH: &str = "docs/contracts/parity-waivers.json";
