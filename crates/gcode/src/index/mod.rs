pub mod api;
#[cfg(test)]
mod api_tests;
pub mod chunker;
pub mod hasher;
pub mod import_resolution;
pub mod indexer;
pub mod languages;
pub mod parser;
pub mod security;
pub mod semantic;
pub mod walker;

/// Maximum file size to index (10 MB).
pub(crate) const MAX_FILE_SIZE: u64 = 10 * 1024 * 1024;
