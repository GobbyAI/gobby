//! Git-aware file discovery using the `ignore` crate.
//! Respects .gitignore and exclude patterns.

mod classification;
mod discovery;
mod generated;
mod hidden;
mod types;

pub use classification::{
    classify_explicit_file_with_options, classify_file, content_language, is_content_indexable,
};
pub use discovery::discover_files_with_options;
pub use types::{DiscoveryOptions, FileClassification};

#[cfg(test)]
pub(crate) use discovery::discover_files;

#[cfg(test)]
mod tests;
