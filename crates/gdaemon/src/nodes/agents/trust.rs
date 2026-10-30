//! Provider trust seeds: the port of `pre_approve_directory` for claude and codex.
use super::repair::{update_claude_config, update_provider_config};
use anyhow::{Context, Result};
use serde_json::{Map, Value};
use std::{
    ffi::OsString,
    fs,
    path::{Path, PathBuf},
};
use toml_edit::{DocumentMut, Item, Table, value};

/// Marks `workspace` and its real path trusted in the provider's store under `home`.
/// Droid, grok and agy have no store.
pub fn seed_trust(provider: &str, home: &Path, workspace: &Path) -> Result<()> {
    let paths = trust_paths(workspace);
    match provider {
        "claude" => seed_claude(home, &paths),
        "codex" => seed_codex(home, &paths),
        _ => Ok(()),
    }
}

/// The workspace as given and, when different, its real path.
fn trust_paths(workspace: &Path) -> Vec<String> {
    let mut paths = vec![workspace.display().to_string()];
    if let Ok(real) = fs::canonicalize(workspace) {
        let real = real.display().to_string();
        if !paths.contains(&real) {
            paths.push(real);
        }
    }
    paths
}

fn seed_claude(home: &Path, paths: &[String]) -> Result<()> {
    let store_path = home.join(".claude.json");
    update_claude_config(&store_path, |store| {
        let projects = store
            .entry("projects")
            .or_insert_with(|| Value::Object(Map::new()))
            .as_object_mut()
            .context("claude projects is not an object")?;
        for path in paths {
            projects
                .entry(path.as_str())
                .or_insert_with(|| Value::Object(Map::new()))
                .as_object_mut()
                .context("claude project entry is not an object")?
                .insert("hasTrustDialogAccepted".into(), Value::Bool(true));
        }
        Ok(())
    })
}

fn seed_codex(home: &Path, paths: &[String]) -> Result<()> {
    let codex_home = home.join(".codex");
    fs::create_dir_all(&codex_home)?;
    let config_path = codex_home.join("config.toml");
    update_provider_config(&config_path, |text| {
        let mut config: DocumentMut = text
            .parse()
            .with_context(|| format!("parse {}", config_path.display()))?;
        let projects = table_entry(
            config
                .entry("projects")
                .or_insert(Item::Table(Table::new())),
        )?;
        // Stale pruning runs only when a requested path is pending, as in trust.py.
        let pending = paths.iter().any(|path| {
            projects
                .get(path)
                .and_then(Item::as_table_like)
                .and_then(|project| project.get("trust_level"))
                .and_then(Item::as_str)
                != Some("trusted")
        });
        if pending {
            // Only deleted generated worktrees, clones and temp workspaces leave; other entries stay.
            let stale: Vec<String> = projects
                .iter()
                .map(|(key, _)| key.to_string())
                .filter(|key| !paths.contains(key) && is_stale_generated_path(Path::new(key)))
                .collect();
            for key in &stale {
                projects.remove(key);
            }
        }
        for path in paths {
            let project = table_entry(projects.entry(path).or_insert(Item::Table(Table::new())))?;
            project["trust_level"] = value("trusted");
        }
        Ok(config.to_string())
    })
}

/// Port of `is_stale_generated_path`: a path that no longer exists under a Gobby-generated
/// root (`~/.gobby/worktrees`, `~/.gobby/clones`, the temp dir, or `/tmp/gobby-*`).
fn is_stale_generated_path(path: &Path) -> bool {
    if path.exists() {
        return false;
    }
    let resolved = realpath(path);
    let gobby = std::env::var_os("HOME").map(|home| Path::new(&home).join(".gobby"));
    let mut roots = vec![std::env::temp_dir()];
    if let Some(gobby) = gobby {
        roots.push(gobby.join("worktrees"));
        roots.push(gobby.join("clones"));
    }
    if roots
        .iter()
        .any(|root| root.is_dir() && resolved.starts_with(realpath(root)))
    {
        return true;
    }
    [realpath(Path::new("/tmp")), PathBuf::from("/private/tmp")]
        .iter()
        .any(|tmp_root| {
            resolved.strip_prefix(tmp_root).is_ok_and(|rest| {
                rest.components()
                    .next()
                    .is_some_and(|first| first.as_os_str().to_string_lossy().starts_with("gobby-"))
            })
        })
}

/// `os.path.realpath` for a path that may not exist: resolves the longest existing prefix.
fn realpath(path: &Path) -> PathBuf {
    let mut existing = path.to_path_buf();
    let mut missing: Vec<OsString> = Vec::new();
    loop {
        if let Ok(real) = fs::canonicalize(&existing) {
            return missing.iter().rev().fold(real, |acc, name| acc.join(name));
        }
        match (existing.file_name(), existing.parent()) {
            (Some(name), Some(parent)) => {
                missing.push(name.to_os_string());
                existing = parent.to_path_buf();
            }
            _ => return path.to_path_buf(),
        }
    }
}

/// Returns `item` as a table, converting an inline table `key = { ... }` in place.
fn table_entry(item: &mut Item) -> Result<&mut Table> {
    if let Some(inline) = item.as_inline_table().cloned() {
        *item = Item::Table(inline.into_table());
    }
    item.as_table_mut()
        .context("trust store entry is not a table")
}
