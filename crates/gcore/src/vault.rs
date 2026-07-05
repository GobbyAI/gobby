//! Shared wiki-vault identity: layout constants, vault detection, and the
//! default vault-directory resolver.
//!
//! Both gcode (codewiki output) and gwiki (vault owner) address the same
//! on-disk vault, so the layout facts live here where neither crate can
//! drift from the other. gwiki re-exports these constants from its
//! `vault.rs`; the Python daemon adopts through #17513.
//!
//! `~/wiki` as the *topic hub* default is a different concept owned by
//! gwiki's scope resolution and is unaffected by this module.
//!
//! The submodules host the shared vault quality core (#17514): [`links`]
//! (wikilink extraction and target normalization), [`mermaid`] (the single
//! Mermaid-validity gate), and [`lint`] (vault-generic checks over pages,
//! parameterized over a [`lint::CitationValidator`]).

use std::path::{Path, PathBuf};

pub mod links;
pub mod lint;
pub mod mermaid;

/// Per-vault control/state dir (scope file, research checkpoint, locks,
/// compile bundles). Underscore-prefixed, not dot-prefixed, so CodeRabbit's
/// minimatch `path_filters` can exclude it (its globstar skips dot segments).
pub const STATE_ROOT: &str = "_gwiki";

/// Scope-identity file inside [`STATE_ROOT`]; its presence marks a directory
/// as an initialized vault.
pub const SCOPE_FILE: &str = "scope.json";

/// Preferred vault directory name inside a project root.
pub const DEFAULT_VAULT_DIR: &str = "wiki";

/// Fallback vault directory name when `wiki/` is occupied by a non-vault.
pub const FALLBACK_VAULT_DIR: &str = "gobby-wiki";

/// Cap on numbered fallback probes (`gobby-wiki-001`…) before resolution
/// gives up. Far beyond any real collision scenario; it only bounds the walk.
const MAX_NUMBERED_FALLBACKS: usize = 999;

/// True when `directory` is an initialized vault: it carries
/// [`STATE_ROOT`]`/`[`SCOPE_FILE`].
pub fn is_vault(directory: &Path) -> bool {
    directory.join(STATE_ROOT).join(SCOPE_FILE).is_file()
}

/// Resolve the vault directory for a project root using the default chain:
///
/// 1. `<root>/wiki` absent → use it (fresh init).
/// 2. `<root>/wiki` exists and is a vault → use it.
/// 3. `<root>/wiki` exists but is not a vault (collision) → `<root>/gobby-wiki`,
///    then `gobby-wiki-001`, `-002`, … applying the same is-vault-or-free test
///    at each step.
///
/// Returns `None` only when every candidate up to the numbered cap is occupied
/// by a non-vault.
pub fn resolve_vault_dir(project_root: &Path) -> Option<PathBuf> {
    let preferred = project_root.join(DEFAULT_VAULT_DIR);
    if is_vault_or_free(&preferred) {
        return Some(preferred);
    }
    let fallback = project_root.join(FALLBACK_VAULT_DIR);
    if is_vault_or_free(&fallback) {
        return Some(fallback);
    }
    (1..=MAX_NUMBERED_FALLBACKS)
        .map(|attempt| project_root.join(format!("{FALLBACK_VAULT_DIR}-{attempt:03}")))
        .find(|candidate| is_vault_or_free(candidate))
}

/// A candidate is usable when it does not exist yet (fresh init claims it) or
/// is already an initialized vault (existing vault wins). Anything else — a
/// non-vault directory or a plain file — is a collision.
fn is_vault_or_free(directory: &Path) -> bool {
    !directory.exists() || is_vault(directory)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn make_vault(directory: &Path) {
        fs::create_dir_all(directory.join(STATE_ROOT)).expect("create state root");
        fs::write(directory.join(STATE_ROOT).join(SCOPE_FILE), "{}\n").expect("write scope file");
    }

    #[test]
    fn is_vault_requires_the_scope_file_not_just_the_state_dir() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        assert!(!is_vault(root));
        fs::create_dir_all(root.join(STATE_ROOT)).expect("create state root");
        assert!(!is_vault(root));
        fs::write(root.join(STATE_ROOT).join(SCOPE_FILE), "{}\n").expect("write scope file");
        assert!(is_vault(root));
    }

    #[test]
    fn fresh_project_resolves_to_wiki() {
        let temp = tempfile::tempdir().expect("tempdir");
        assert_eq!(
            resolve_vault_dir(temp.path()),
            Some(temp.path().join("wiki"))
        );
    }

    #[test]
    fn existing_wiki_vault_wins() {
        let temp = tempfile::tempdir().expect("tempdir");
        make_vault(&temp.path().join("wiki"));
        assert_eq!(
            resolve_vault_dir(temp.path()),
            Some(temp.path().join("wiki"))
        );
    }

    #[test]
    fn non_vault_wiki_collision_falls_back_to_gobby_wiki() {
        let temp = tempfile::tempdir().expect("tempdir");
        fs::create_dir_all(temp.path().join("wiki")).expect("create collision dir");
        assert_eq!(
            resolve_vault_dir(temp.path()),
            Some(temp.path().join("gobby-wiki"))
        );
    }

    #[test]
    fn wiki_collision_as_plain_file_also_falls_back() {
        let temp = tempfile::tempdir().expect("tempdir");
        fs::write(temp.path().join("wiki"), "not a directory").expect("write collision file");
        assert_eq!(
            resolve_vault_dir(temp.path()),
            Some(temp.path().join("gobby-wiki"))
        );
    }

    #[test]
    fn existing_gobby_wiki_vault_wins_over_fresh_creation() {
        let temp = tempfile::tempdir().expect("tempdir");
        fs::create_dir_all(temp.path().join("wiki")).expect("create collision dir");
        make_vault(&temp.path().join("gobby-wiki"));
        assert_eq!(
            resolve_vault_dir(temp.path()),
            Some(temp.path().join("gobby-wiki"))
        );
    }

    #[test]
    fn occupied_gobby_wiki_advances_to_numbered_fallback() {
        let temp = tempfile::tempdir().expect("tempdir");
        fs::create_dir_all(temp.path().join("wiki")).expect("create collision dir");
        fs::create_dir_all(temp.path().join("gobby-wiki")).expect("create second collision");
        assert_eq!(
            resolve_vault_dir(temp.path()),
            Some(temp.path().join("gobby-wiki-001"))
        );
    }

    #[test]
    fn numbered_fallback_prefers_an_existing_vault_over_a_fresh_slot() {
        let temp = tempfile::tempdir().expect("tempdir");
        fs::create_dir_all(temp.path().join("wiki")).expect("create collision dir");
        fs::create_dir_all(temp.path().join("gobby-wiki")).expect("create second collision");
        make_vault(&temp.path().join("gobby-wiki-001"));
        assert_eq!(
            resolve_vault_dir(temp.path()),
            Some(temp.path().join("gobby-wiki-001"))
        );
    }

    #[test]
    fn numbered_fallback_skips_occupied_slots() {
        let temp = tempfile::tempdir().expect("tempdir");
        fs::create_dir_all(temp.path().join("wiki")).expect("create collision dir");
        fs::create_dir_all(temp.path().join("gobby-wiki")).expect("create second collision");
        fs::create_dir_all(temp.path().join("gobby-wiki-001")).expect("create third collision");
        assert_eq!(
            resolve_vault_dir(temp.path()),
            Some(temp.path().join("gobby-wiki-002"))
        );
    }
}
