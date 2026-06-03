//! Source manifest records for immutable raw wiki sources.

use std::time::Duration;

mod atomic;
mod manifest;
mod render;
mod types;

pub use manifest::*;
pub use types::*;

const SOURCE_ID_HASH_PREFIX_LEN: usize = 16;
const SOURCE_MANIFEST_LOCK_TIMEOUT_ENV: &str = "GWIKI_SOURCE_MANIFEST_LOCK_TIMEOUT_MS";
const DEFAULT_SOURCE_MANIFEST_LOCK_TIMEOUT: Duration = Duration::from_secs(30);
const SOURCE_MANIFEST_LOCK_RETRY_DELAY: Duration = Duration::from_millis(25);

const SOURCE_MARKER: &str = "<!-- gwiki-source:";
const GENERATED_SOURCE_MANIFEST_START: &str = "<!-- GENERATED SOURCE MANIFEST START -->";
const GENERATED_SOURCE_MANIFEST_END: &str = "<!-- GENERATED SOURCE MANIFEST END -->";

#[cfg(test)]
mod tests;
