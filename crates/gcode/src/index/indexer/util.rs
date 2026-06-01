use std::borrow::Cow;
use std::collections::BTreeMap;
use std::ffi::OsString;
use std::path::{Path, PathBuf};

use super::types::UnsupportedFileType;

/// Default exclude patterns (matching Python CodeIndexConfig defaults).
pub(super) const DEFAULT_EXCLUDES: &[&str] = &[
    "node_modules",
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    "dist",
    "build",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "target",
    ".next",
    ".nuxt",
    "coverage",
    ".cache",
];

pub(super) fn filter_discovered_paths(
    root_path: &Path,
    path_filter: &Path,
    paths: Vec<PathBuf>,
) -> Vec<PathBuf> {
    let filter_abs = if path_filter.is_absolute() {
        Cow::Borrowed(path_filter)
    } else {
        Cow::Owned(root_path.join(path_filter))
    };
    let filter_canonical = filter_abs.canonicalize().ok();

    paths
        .into_iter()
        .filter(|path| {
            let path_abs = if path.is_absolute() {
                Cow::Borrowed(path.as_path())
            } else {
                Cow::Owned(root_path.join(path))
            };
            if path_abs.as_ref() == filter_abs.as_ref()
                || path_abs.as_ref().starts_with(filter_abs.as_ref())
            {
                return true;
            }

            let Some(filter_canonical) = &filter_canonical else {
                return false;
            };
            path_abs.canonicalize().is_ok_and(|path_canonical| {
                path_canonical == *filter_canonical || path_canonical.starts_with(filter_canonical)
            })
        })
        .collect()
}

const UNSUPPORTED_EXAMPLES_PER_TYPE: usize = 5;

pub(super) fn unsupported_file_types(
    root_path: &Path,
    paths: &[PathBuf],
) -> Vec<UnsupportedFileType> {
    let mut grouped = BTreeMap::<String, UnsupportedFileType>::new();
    for path in paths {
        let extension = unsupported_file_type_label(path);
        let entry = grouped
            .entry(extension.clone())
            .or_insert_with(|| UnsupportedFileType {
                extension,
                files: 0,
                examples: Vec::new(),
            });
        entry.files += 1;
        if entry.examples.len() < UNSUPPORTED_EXAMPLES_PER_TYPE
            && let Ok(rel) = relative_path(path, root_path)
        {
            entry.examples.push(rel);
        }
    }

    grouped.into_values().collect()
}

fn unsupported_file_type_label(path: &Path) -> String {
    path.extension()
        .and_then(|ext| ext.to_str())
        .filter(|ext| !ext.is_empty())
        .map(|ext| format!(".{}", ext.to_lowercase()))
        .unwrap_or_else(|| "extensionless".to_string())
}

pub(super) fn requested_relative_path(root_path: &Path, requested_path: &Path) -> String {
    if requested_path.is_absolute() {
        return requested_path.strip_prefix(root_path).map_or_else(
            |_| lexical_relative_path(root_path, requested_path),
            |relative| relative.to_string_lossy().to_string(),
        );
    }
    requested_path.to_string_lossy().to_string()
}

fn lexical_relative_path(root_path: &Path, requested_path: &Path) -> String {
    let root_parts = normalized_components(root_path);
    let requested_parts = normalized_components(requested_path);
    let common_len = root_parts
        .iter()
        .zip(&requested_parts)
        .take_while(|(left, right)| left == right)
        .count();

    if common_len == 0 {
        return requested_parts
            .into_iter()
            .collect::<PathBuf>()
            .to_string_lossy()
            .to_string();
    }

    let mut relative = PathBuf::new();
    for _ in common_len..root_parts.len() {
        relative.push("..");
    }
    for part in requested_parts.into_iter().skip(common_len) {
        relative.push(part);
    }
    if relative.as_os_str().is_empty() {
        ".".to_string()
    } else {
        relative.to_string_lossy().to_string()
    }
}

fn normalized_components(path: &Path) -> Vec<OsString> {
    path.components()
        .filter_map(|component| match component {
            std::path::Component::Prefix(prefix) => Some(prefix.as_os_str().to_os_string()),
            std::path::Component::RootDir | std::path::Component::CurDir => None,
            std::path::Component::ParentDir => Some(OsString::from("..")),
            std::path::Component::Normal(value) => Some(value.to_os_string()),
        })
        .collect()
}

pub(super) fn relative_path(path: &Path, root: &Path) -> anyhow::Result<String> {
    let abs = path.canonicalize()?;
    let root_abs = root.canonicalize()?;
    Ok(abs.strip_prefix(&root_abs)?.to_string_lossy().to_string())
}

pub(super) fn epoch_secs_str() -> String {
    use std::time::SystemTime;
    let secs = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    secs.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn filter_discovered_paths_uses_lexical_match_before_canonicalizing() {
        let root = Path::new("/tmp/project");
        let paths = vec![
            PathBuf::from("/tmp/project/src/lib.rs"),
            PathBuf::from("/tmp/project/tests/lib.rs"),
        ];

        let filtered = filter_discovered_paths(root, Path::new("src"), paths);

        assert_eq!(filtered, vec![PathBuf::from("/tmp/project/src/lib.rs")]);
    }

    #[test]
    fn requested_relative_path_uses_relative_diff_for_absolute_paths_outside_root() {
        let root = Path::new("/tmp/project");
        let requested = Path::new("/tmp/other/file.rs");

        assert_eq!(requested_relative_path(root, requested), "../other/file.rs");
    }
}
