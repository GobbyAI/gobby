use std::env;
use std::path::{Component, Path, PathBuf};

use crate::WikiError;

use super::{SourceManifest, SourceReplay};

const SCRATCHPAD_COMPONENT_NAMES: &[&str] = &[
    "scratchpad",
    "scratchpads",
    "agent-scratchpad",
    "agent-scratchpads",
    "gobby-scratchpad",
    "gobby-scratchpads",
    "gobby-agent-scratchpad",
    "gobby-agent-scratchpads",
    "codex-scratchpad",
    "codex-scratchpads",
    "claude-scratchpad",
    "claude-scratchpads",
];

const SCRATCHPAD_COMPONENT_PREFIXES: &[&str] = &[
    "scratchpad-",
    "scratchpad_",
    "scratchpads-",
    "scratchpads_",
    "agent-scratchpad-",
    "agent-scratchpad_",
    "gobby-scratchpad-",
    "gobby-scratchpad_",
    "gobby-agent-scratchpad-",
    "gobby-agent-scratchpad_",
    "codex-scratchpad-",
    "codex-scratchpad_",
    "claude-scratchpad-",
    "claude-scratchpad_",
];

pub(crate) fn read_manifest_with_ephemeral_scratchpad_replay_repair(
    vault_root: &Path,
    dry_run: bool,
) -> Result<SourceManifest, WikiError> {
    if dry_run {
        let mut manifest = SourceManifest::read(vault_root)?;
        strip_ephemeral_scratchpad_replay_entries(&mut manifest);
        return Ok(manifest);
    }

    SourceManifest::update(vault_root, |manifest| {
        Ok(strip_ephemeral_scratchpad_replay_entries(manifest))
    })?;
    SourceManifest::read(vault_root)
}

pub(crate) fn strip_ephemeral_scratchpad_replay_entries(manifest: &mut SourceManifest) -> bool {
    let mut changed = false;
    for entry in &mut manifest.entries {
        let Some(SourceReplay::LocalFile { path, .. }) = entry.replay.as_ref() else {
            continue;
        };
        if is_ephemeral_scratchpad_replay_path(path) {
            entry.replay = None;
            changed = true;
        }
    }
    changed
}

pub(crate) fn is_ephemeral_scratchpad_replay_path(path: &Path) -> bool {
    path.is_absolute() && is_under_known_temp_root(path) && has_scratchpad_component(path)
}

fn is_under_known_temp_root(path: &Path) -> bool {
    known_temp_roots().iter().any(|root| path.starts_with(root))
}

fn known_temp_roots() -> Vec<PathBuf> {
    let mut roots = vec![
        env::temp_dir(),
        PathBuf::from("/tmp"),
        PathBuf::from("/private/tmp"),
    ];
    roots.sort();
    roots.dedup();
    roots
}

fn has_scratchpad_component(path: &Path) -> bool {
    path.components().any(|component| {
        let Component::Normal(value) = component else {
            return false;
        };
        value.to_str().map(is_scratchpad_component).unwrap_or(false)
    })
}

fn is_scratchpad_component(component: &str) -> bool {
    let normalized = component.to_ascii_lowercase();
    SCRATCHPAD_COMPONENT_NAMES.contains(&normalized.as_str())
        || SCRATCHPAD_COMPONENT_PREFIXES
            .iter()
            .any(|prefix| normalized.starts_with(prefix))
}
