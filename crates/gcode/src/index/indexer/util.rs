use std::path::{Path, PathBuf};

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
        path_filter.to_path_buf()
    } else {
        root_path.join(path_filter)
    };
    let filter_canonical = filter_abs.canonicalize().ok();

    paths
        .into_iter()
        .filter(|path| {
            let path_abs = if path.is_absolute() {
                path.clone()
            } else {
                root_path.join(path)
            };
            if path_abs == filter_abs || path_abs.starts_with(&filter_abs) {
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

pub(super) fn requested_relative_path(root_path: &Path, requested_path: &Path) -> String {
    if requested_path.is_absolute() {
        return requested_path
            .strip_prefix(root_path)
            .unwrap_or(requested_path)
            .to_string_lossy()
            .to_string();
    }
    requested_path.to_string_lossy().to_string()
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
}
