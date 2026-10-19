//! Pane detection manifests, the Rust side of `gobby.agents.detection` (#24381, plan 2.12).

mod attention;
mod casefold_table;
mod digits;
mod idle;
mod loop_tracker;
mod manifest;
mod matcher;
mod prompt;
mod quota;
mod regions;
mod stall;

#[cfg(test)]
mod tests;

use std::collections::{BTreeSet, HashMap};

pub use attention::{
    AttentionClassification, AttentionInput, AttentionTransition, classify_attention,
};
pub use idle::{ComposerRead, ComposerState, IdleDetector};
pub use loop_tracker::LoopTracker;
pub use manifest::{DetectionState, ManifestError};
pub use matcher::{
    CompiledManifest, DetectionMatch, IssueCode, ManifestIssue, MatchEvaluation, compile_manifest,
};
pub use prompt::{DetectedPrompt, PromptDetector, PromptKind};
pub use quota::{ProviderQuotaExhaustion, capacity_pane_message, detect_provider_quota};
pub use stall::{StallClassification, StallClassifier, StallRunState, StallStatus};

/// Per-run detector state the caller owns (#24382, plan 2.14). Hub-side policy counters, such as
/// reprompts and reset-idle, stay with the hub and are not part of it.
#[derive(Debug, Default)]
pub struct RunDetectState {
    pub stall: StallRunState,
    pub loop_dismissals: u32,
    trust_dismissed: bool,
    approval_fingerprint: Option<String>,
    loop_prompt_fingerprints: BTreeSet<String>,
}

impl RunDetectState {
    pub fn new() -> Self {
        Self::default()
    }

    /// Records that the run's trust prompt was dismissed, as `PromptDetector.mark_dismissed`.
    pub fn mark_trust_dismissed(&mut self) {
        self.trust_dismissed = true;
    }

    /// Records the approval prompt already handled for this run, as
    /// `PromptDetector.mark_approval_prompt_dismissed`.
    pub fn mark_approval_prompt_dismissed(&mut self, pane: &str) {
        self.approval_fingerprint = Some(prompt::pane_fingerprint(pane));
    }

    /// Stub (#24382 correction, RED): the dismissal getters and loop dismissal are not yet
    /// implemented.
    pub fn was_trust_dismissed(&self) -> bool {
        false
    }

    pub fn was_approval_prompt_dismissed(&self, _pane: &str) -> bool {
        false
    }

    pub fn mark_loop_prompt_dismissed(&mut self, _pane: &str) {}

    pub fn was_loop_prompt_dismissed(&self, _pane: &str) -> bool {
        false
    }
}

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
