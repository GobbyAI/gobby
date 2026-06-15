use std::collections::BTreeSet;
use std::path::{Component, Path, PathBuf};

const GCODE_CONFIG_PATH: &str = ".gobby/gcode.json";
const DEFAULT_HIDDEN_ALLOWLIST_PATTERNS: &[&str] = &[
    ".gobby/plans/**/*.md",
    ".gobby/wiki/**/*.md",
    ".github/workflows/**/*.yml",
    ".github/workflows/**/*.yaml",
];

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct HiddenPathAllowlist {
    patterns: Vec<String>,
}

impl HiddenPathAllowlist {
    pub(super) fn load(root: &Path) -> Self {
        let mut patterns = DEFAULT_HIDDEN_ALLOWLIST_PATTERNS
            .iter()
            .map(|pattern| (*pattern).to_string())
            .collect::<Vec<_>>();
        patterns.extend(read_project_hidden_allowlist(root));
        Self::from_patterns(patterns)
    }

    fn from_patterns(patterns: Vec<String>) -> Self {
        let patterns = patterns
            .into_iter()
            .map(|pattern| pattern.trim().replace('\\', "/"))
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
                if entry.is_file() && is_hidden_path(root, &entry) {
                    paths.insert(entry);
                }
            }
        }
        paths.into_iter().collect()
    }

    pub(super) fn matches(&self, root: &Path, path: &Path) -> bool {
        let rel = path.strip_prefix(root).unwrap_or(path);
        let rel = rel.to_string_lossy().replace('\\', "/");
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

pub(super) fn is_hidden_metadata_content_only(root: &Path, path: &Path) -> bool {
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

    if components.len() >= 3
        && components[0] == ".gobby"
        && components[1] == "wiki"
        && path_has_extension(path, &["md"])
    {
        return true;
    }

    components.len() >= 3
        && components[0] == ".github"
        && components[1] == "workflows"
        && path_has_extension(path, &["yml", "yaml"])
}

pub(super) fn is_generated_wiki_metadata(root: &Path, path: &Path) -> bool {
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
        && components[1] == "wiki"
        && components[2] == "_meta"
    {
        return true;
    }

    components.len() >= 3
        && components[0] == ".gobby"
        && components[1] == "wiki"
        && path
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name.ends_with(".json.lock"))
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
