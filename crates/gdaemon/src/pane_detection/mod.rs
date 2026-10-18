//! Pane detection manifests, the Rust side of `gobby.agents.detection` (#24381, plan 2.12).

mod manifest;
mod matcher;
mod regions;

use std::collections::HashMap;

pub use manifest::{DetectionState, ManifestError};
pub use matcher::{
    CompiledManifest, DetectionMatch, IssueCode, ManifestIssue, MatchEvaluation, compile_manifest,
};

/// Compiled manifests keyed by provider id, as the Python registry holds them.
#[derive(Debug, Default)]
pub struct ManifestSet {
    manifests: HashMap<String, CompiledManifest>,
}

impl ManifestSet {
    /// Compiles `(provider_id, content)` rows as `_install_rows` does. A row that fails to
    /// compile, or whose manifest names another provider, is skipped with a warning on stderr.
    /// A later row for the same provider replaces an earlier one.
    pub fn from_rows(rows: &[(String, String)]) -> Self {
        Self::from_rows_logged(rows, |line| eprintln!("gdaemon: {line}"))
    }

    /// `from_rows` with each skipped row reported through `warn`, in the same
    /// `Skipping invalid detection manifest` form Python logs.
    pub fn from_rows_logged(rows: &[(String, String)], mut warn: impl FnMut(&str)) -> Self {
        let mut manifests = HashMap::new();
        for (provider_id, content) in rows {
            let error = match compile_manifest(content) {
                Ok(compiled) if compiled.manifest_id() == provider_id.as_str() => {
                    manifests.insert(provider_id.clone(), compiled);
                    continue;
                }
                Ok(compiled) => format!(
                    "row provider {provider_id:?} disagrees with manifest id {:?}",
                    compiled.manifest_id()
                ),
                Err(err) => err.message().to_owned(),
            };
            warn(&format!(
                "Skipping invalid detection manifest provider_id={provider_id} error={error}"
            ));
        }
        Self { manifests }
    }

    pub fn for_provider(&self, provider_id: &str) -> Option<&CompiledManifest> {
        self.manifests.get(provider_id)
    }
}
