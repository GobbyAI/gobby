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
    /// compile, or whose manifest names another provider, is skipped. A later row for the same
    /// provider replaces an earlier one.
    pub fn from_rows(rows: &[(String, String)]) -> Self {
        let mut manifests = HashMap::new();
        for (provider_id, content) in rows {
            let Ok(compiled) = compile_manifest(content) else {
                continue;
            };
            if compiled.manifest_id() != provider_id.as_str() {
                continue;
            }
            manifests.insert(provider_id.clone(), compiled);
        }
        Self { manifests }
    }

    pub fn for_provider(&self, provider_id: &str) -> Option<&CompiledManifest> {
        self.manifests.get(provider_id)
    }
}
