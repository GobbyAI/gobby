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
    let filter_abs = filter_abs.canonicalize().unwrap_or(filter_abs);

    paths
        .into_iter()
        .filter(|path| {
            let path_abs = path.canonicalize().unwrap_or_else(|_| path.clone());
            path_abs == filter_abs || path_abs.starts_with(&filter_abs)
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
    format!("{secs}")
}
