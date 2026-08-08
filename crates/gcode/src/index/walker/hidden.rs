use std::collections::BTreeSet;
use std::path::{Component, Path, PathBuf};

use gobby_core::vault::resolve_vault_dir;

use crate::index::{normalize_storage_path, normalize_storage_path_str};

const GCODE_CONFIG_PATH: &str = ".gobby/gcode.json";
const DEFAULT_HIDDEN_ALLOWLIST_PATTERNS: &[&str] = &[
    ".gobby/plans/**/*.md",
    ".github/workflows/**/*.yml",
    ".github/workflows/**/*.yaml",
];

fn vault_dir_name(root: &Path) -> Option<String> {
    resolve_vault_dir(root)?
        .file_name()
        .and_then(|name| name.to_str())
        .map(str::to_owned)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct HiddenPathContext {
    vault_dir_name: Option<String>,
    allowlist: HiddenPathAllowlist,
}

impl HiddenPathContext {
    pub(super) fn load(root: &Path) -> Self {
        let vault_dir_name = vault_dir_name(root);
        let allowlist = HiddenPathAllowlist::load_with_vault(root, vault_dir_name.as_deref());
        Self {
            vault_dir_name,
            allowlist,
        }
    }

    pub(super) fn allowlist(&self) -> &HiddenPathAllowlist {
        &self.allowlist
    }

    fn vault_dir_name(&self) -> Option<&str> {
        self.vault_dir_name.as_deref()
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct HiddenPathAllowlist {
    patterns: Vec<String>,
}

impl HiddenPathAllowlist {
    fn load_with_vault(root: &Path, vault_dir_name: Option<&str>) -> Self {
        let mut patterns = DEFAULT_HIDDEN_ALLOWLIST_PATTERNS
            .iter()
            .map(|pattern| (*pattern).to_string())
            .collect::<Vec<_>>();
        if let Some(vault) = vault_dir_name {
            patterns.push(format!("{vault}/**/*.md"));
        }
        patterns.extend(read_project_hidden_allowlist(root));
        Self::from_patterns(patterns)
    }

    fn from_patterns(patterns: Vec<String>) -> Self {
        let patterns = patterns
            .into_iter()
            .map(|pattern| normalize_storage_path_str(pattern.trim()))
            .filter(|pattern| is_valid_allowlist_pattern(pattern))
            .flat_map(|pattern| expand_zero_depth_globstar(&pattern))
            .collect();
        Self { patterns }
    }

    pub(super) fn discover(&self, root: &Path) -> Vec<PathBuf> {
        let mut paths = BTreeSet::new();
        for pattern in &self.patterns {
            let Some(abs_pattern) = absolute_glob_pattern(root, pattern) else {
                continue;
            };
            let Ok(entries) = glob::glob(&abs_pattern) else {
                continue;
            };
            for entry in entries.flatten() {
                // No hidden-path gate: allowlisted files skipped by the main
                // walk for ANY reason (hidden dirs like .gobby, or gitignored
                // ones like the wiki vault) are rescued here; files the walk
                // already yielded dedup via the caller's `seen` set.
                if entry.is_file() {
                    paths.insert(entry);
                }
            }
        }
        paths.into_iter().collect()
    }

    pub(super) fn matches(&self, root: &Path, path: &Path) -> bool {
        let rel = path.strip_prefix(root).unwrap_or(path);
        let rel = normalize_storage_path(rel);
        self.patterns.iter().any(|pattern| {
            glob::Pattern::new(pattern)
                .map(|pattern| pattern.matches_path(Path::new(&rel)))
                .unwrap_or(false)
        })
    }
}

fn read_project_hidden_allowlist(root: &Path) -> Vec<String> {
    let Ok(contents) = std::fs::read_to_string(root.join(GCODE_CONFIG_PATH)) else {
        return Vec::new();
    };
    let Ok(json) = serde_json::from_str::<serde_json::Value>(&contents) else {
        return Vec::new();
    };
    json.get("index")
        .and_then(|index| index.get("hidden_allowlist"))
        .and_then(|allowlist| allowlist.as_array())
        .into_iter()
        .flatten()
        .filter_map(|value| value.as_str().map(ToOwned::to_owned))
        .collect()
}

fn is_valid_allowlist_pattern(pattern: &str) -> bool {
    if pattern.is_empty() {
        return false;
    }
    let path = Path::new(pattern);
    !path.is_absolute()
        && !path.components().any(|component| {
            matches!(
                component,
                Component::ParentDir | Component::Prefix(_) | Component::RootDir
            )
        })
}

fn expand_zero_depth_globstar(pattern: &str) -> Vec<String> {
    let mut expanded = vec![pattern.to_string()];
    if let Some((prefix, suffix)) = pattern.split_once("/**/") {
        expanded.push(format!("{prefix}/{suffix}"));
    }
    expanded
}

fn absolute_glob_pattern(root: &Path, pattern: &str) -> Option<String> {
    let root = root.to_str()?;
    Some(format!("{}/{}", glob::Pattern::escape(root), pattern))
}

pub(super) fn is_hidden_path(root: &Path, path: &Path) -> bool {
    let rel = path.strip_prefix(root).unwrap_or(path);
    rel.components().any(|component| {
        component
            .as_os_str()
            .to_str()
            .is_some_and(|name| name.starts_with('.') && name != "." && name != "..")
    })
}

pub(super) fn is_hidden_metadata_content_only_with_context(
    root: &Path,
    path: &Path,
    context: &HiddenPathContext,
) -> bool {
    let rel = path.strip_prefix(root).unwrap_or(path);
    let components = rel
        .components()
        .filter_map(|component| match component {
            Component::Normal(value) => value.to_str(),
            _ => None,
        })
        .collect::<Vec<_>>();

    if components.len() >= 3
        && components[0] == ".gobby"
        && components[1] == "plans"
        && path_has_extension(path, &["md"])
    {
        return true;
    }

    if components.len() >= 2
        && path_has_extension(path, &["md"])
        && context.vault_dir_name() == Some(components[0])
    {
        return true;
    }

    components.len() >= 3
        && components[0] == ".github"
        && components[1] == "workflows"
        && path_has_extension(path, &["yml", "yaml"])
}

pub(super) fn is_generated_wiki_metadata_with_context(
    root: &Path,
    path: &Path,
    context: &HiddenPathContext,
) -> bool {
    let rel = path.strip_prefix(root).unwrap_or(path);
    let components = rel
        .components()
        .filter_map(|component| match component {
            Component::Normal(value) => value.to_str(),
            _ => None,
        })
        .collect::<Vec<_>>();

    let Some(vault) = context.vault_dir_name() else {
        return false;
    };
    if components.first().copied() != Some(vault) {
        return false;
    }

    if components.get(1).copied() == Some("_meta")
        || components.get(1).copied() == Some(".obsidian")
        || components.get(1).copied() == Some(gobby_core::vault::STATE_ROOT)
    {
        return true;
    }

    if components.len() == 2 && components[1] == "wikis.json" {
        return true;
    }

    path.file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.ends_with(".lock"))
}

fn path_has_extension(path: &Path, extensions: &[&str]) -> bool {
    path.extension()
        .and_then(|extension| extension.to_str())
        .map(|extension| {
            let extension = extension.to_ascii_lowercase();
            extensions.contains(&extension.as_str())
        })
        .unwrap_or(false)
}
