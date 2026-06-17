//! Obsidian-vault integration for `gwiki init`.
//!
//! Two pieces of hardening run at init time:
//!
//! 1. [`seed_app_json`] writes `<vault>/.obsidian/app.json` with a
//!    `userIgnoreFilters` entry for the control dir ([`crate::vault::STATE_ROOT`])
//!    so it is de-noised in Obsidian's search/graph.
//! 2. [`ensure_gitignore_obsidian`] adds `.obsidian/` to the project's
//!    `.gitignore` — but only inside a git work tree. Obsidian's workspace config
//!    is machine-local, and `.obsidian/` is itself a dot-dir that CodeRabbit's
//!    minimatch `path_filters` cannot exclude, so it must never be committed.

use std::path::{Path, PathBuf};

use serde_json::{Map, Value};

use crate::WikiError;
use crate::vault::STATE_ROOT;

/// The `userIgnoreFilters` pattern that hides the control dir in Obsidian.
fn state_filter() -> String {
    format!("{STATE_ROOT}/")
}

/// Seed `<vault_root>/.obsidian/app.json` with a `userIgnoreFilters` entry for the
/// control dir.
///
/// Idempotent and non-destructive: merges into any existing `app.json`, preserving
/// unrelated keys and existing filters. Obsidian owns this file and rewrites it on
/// every settings change, so we never overwrite it wholesale. If the existing file
/// is shaped unexpectedly (not an object, or `userIgnoreFilters` is not an array),
/// we leave it untouched rather than fight Obsidian.
pub(crate) fn seed_app_json(vault_root: &Path) -> Result<(), WikiError> {
    let dir = vault_root.join(".obsidian");
    let path = dir.join("app.json");
    let filter = state_filter();

    let mut root = match std::fs::read(&path) {
        Ok(bytes) => serde_json::from_slice::<Value>(&bytes).map_err(|source| WikiError::Json {
            action: "parse obsidian app.json",
            path: Some(path.clone()),
            source,
        })?,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Value::Object(Map::new()),
        Err(source) => {
            return Err(WikiError::Io {
                action: "read obsidian app.json",
                path: Some(path.clone()),
                source,
            });
        }
    };

    let Some(object) = root.as_object_mut() else {
        return Ok(());
    };
    let filters = object
        .entry("userIgnoreFilters")
        .or_insert_with(|| Value::Array(Vec::new()));
    let Some(filters) = filters.as_array_mut() else {
        return Ok(());
    };
    if filters.iter().any(|e| e.as_str() == Some(filter.as_str())) {
        return Ok(());
    }
    filters.push(Value::String(filter));

    let serialized = serde_json::to_vec_pretty(&root).map_err(|source| WikiError::Json {
        action: "serialize obsidian app.json",
        path: Some(path.clone()),
        source,
    })?;

    std::fs::create_dir_all(&dir).map_err(|source| WikiError::Io {
        action: "create obsidian config directory",
        path: Some(dir.clone()),
        source,
    })?;
    std::fs::write(&path, serialized).map_err(|source| WikiError::Io {
        action: "write obsidian app.json",
        path: Some(path),
        source,
    })
}

/// Ensure `.obsidian/` is git-ignored, but only when `project_root` lives inside a
/// git work tree (not every wiki lives in a code project).
///
/// The rule is appended at end-of-file: git uses last-match-wins, so a `.gitignore`
/// that re-includes the vault (e.g. `!.gobby/wiki/**`) would otherwise keep tracking
/// the vault's `.obsidian/`. The entry is generic (`.obsidian/`), so it matches the
/// vault's nested `.obsidian` at any depth. Creates the `.gitignore` if absent.
/// Idempotent: a no-op when an `.obsidian` rule already exists.
pub(crate) fn ensure_gitignore_obsidian(project_root: &Path) -> Result<(), WikiError> {
    let Some(git_root) = find_git_root(project_root) else {
        return Ok(());
    };
    let gitignore = git_root.join(".gitignore");

    let existing = match std::fs::read_to_string(&gitignore) {
        Ok(text) => Some(text),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
        Err(source) => {
            return Err(WikiError::Io {
                action: "read .gitignore",
                path: Some(gitignore.clone()),
                source,
            });
        }
    };

    if let Some(text) = &existing
        && text
            .lines()
            .any(|line| matches!(line.trim(), ".obsidian" | ".obsidian/"))
    {
        return Ok(());
    }

    let mut next = existing.unwrap_or_default();
    if !next.is_empty() && !next.ends_with('\n') {
        next.push('\n');
    }
    next.push_str(".obsidian/\n");

    std::fs::write(&gitignore, next).map_err(|source| WikiError::Io {
        action: "write .gitignore",
        path: Some(gitignore),
        source,
    })
}

/// Walk up from `start` for a `.git` entry — a directory for normal repos, a file
/// for worktrees and submodules. Returns the directory that contains it, or `None`.
fn find_git_root(start: &Path) -> Option<PathBuf> {
    let mut current = Some(start);
    while let Some(dir) = current {
        if dir.join(".git").exists() {
            return Some(dir.to_path_buf());
        }
        current = dir.parent();
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn seed_app_json_creates_filter_when_absent() {
        let temp = tempfile::tempdir().expect("tempdir");
        let vault = temp.path();
        seed_app_json(vault).expect("seed");
        let text =
            std::fs::read_to_string(vault.join(".obsidian/app.json")).expect("read app.json");
        assert!(text.contains("userIgnoreFilters"));
        assert!(text.contains("_gwiki/"));
    }

    #[test]
    fn seed_app_json_is_idempotent_and_preserves_keys() {
        let temp = tempfile::tempdir().expect("tempdir");
        let vault = temp.path();
        let obsidian = vault.join(".obsidian");
        std::fs::create_dir_all(&obsidian).expect("mkdir");
        std::fs::write(
            obsidian.join("app.json"),
            r#"{"promptDelete":false,"userIgnoreFilters":["existing/"]}"#,
        )
        .expect("seed existing");

        seed_app_json(vault).expect("seed once");
        seed_app_json(vault).expect("seed twice");

        let text = std::fs::read_to_string(obsidian.join("app.json")).expect("read app.json");
        let value: Value = serde_json::from_str(&text).expect("parse");
        let filters = value["userIgnoreFilters"].as_array().expect("array");
        assert_eq!(
            filters.iter().filter(|e| *e == "_gwiki/").count(),
            1,
            "filter added exactly once"
        );
        assert!(
            filters.iter().any(|e| *e == "existing/"),
            "existing filter kept"
        );
        assert_eq!(
            value["promptDelete"],
            Value::Bool(false),
            "unrelated key kept"
        );
    }

    #[test]
    fn gitignore_noop_outside_git_work_tree() {
        let temp = tempfile::tempdir().expect("tempdir");
        ensure_gitignore_obsidian(temp.path()).expect("noop");
        assert!(!temp.path().join(".gitignore").exists());
    }

    #[test]
    fn gitignore_appends_once_and_preserves_existing() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        std::fs::create_dir_all(root.join(".git")).expect("fake git");
        std::fs::write(root.join(".gitignore"), "/target\n.claude/\n").expect("seed gitignore");

        ensure_gitignore_obsidian(root).expect("first");
        ensure_gitignore_obsidian(root).expect("second");

        let text = std::fs::read_to_string(root.join(".gitignore")).expect("read");
        assert_eq!(
            text.lines().filter(|l| l.trim() == ".obsidian/").count(),
            1,
            "rule appended exactly once"
        );
        assert!(text.contains("/target"), "existing content preserved");
        assert!(
            text.trim_end().ends_with(".obsidian/"),
            "rule lands at end of file"
        );
    }

    #[test]
    fn gitignore_created_when_absent_in_git_tree() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        std::fs::create_dir_all(root.join(".git")).expect("fake git");
        ensure_gitignore_obsidian(root).expect("create");
        let text = std::fs::read_to_string(root.join(".gitignore")).expect("read");
        assert_eq!(text, ".obsidian/\n");
    }
}
