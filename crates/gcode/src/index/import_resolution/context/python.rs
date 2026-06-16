use std::collections::HashSet;
use std::path::{Path, PathBuf};

pub(in crate::index::import_resolution) fn build_python_module_index(
    root_path: &Path,
    candidate_files: &[PathBuf],
) -> HashSet<String> {
    let mut modules = HashSet::new();

    for path in candidate_files {
        modules.extend(python_module_names_for_path(root_path, path));
    }

    modules
}

/// Project-relative files a Python dotted `module` could be defined in, derived
/// by inverting [`python_module_names_for_path`] — no file reads. Covers both
/// `pkg/mod.py` and the package form `pkg/mod/__init__.py`, the `.pyi` stub
/// variants, and a `src/`-layout prefix. The post-write pass narrows these
/// against the actually-indexed symbols, so listing non-existent paths is safe.
pub(in crate::index::import_resolution) fn python_candidate_files(module: &str) -> Vec<String> {
    let base = module.replace('.', "/");
    let mut files = Vec::with_capacity(8);
    for stem in [&base, &format!("src/{base}")] {
        for ext in ["py", "pyi"] {
            files.push(format!("{stem}.{ext}"));
            files.push(format!("{stem}/__init__.{ext}"));
        }
    }
    files
}

fn python_module_names_for_path(root_path: &Path, path: &Path) -> Vec<String> {
    let Ok(rel) = path.strip_prefix(root_path) else {
        return Vec::new();
    };
    let ext = rel
        .extension()
        .and_then(|ext| ext.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    if !matches!(ext.as_str(), "py" | "pyi") {
        return Vec::new();
    }

    let mut module = rel
        .with_extension("")
        .to_string_lossy()
        .replace(['/', '\\'], ".");
    if module.ends_with(".__init__") {
        module.truncate(module.len() - ".__init__".len());
    }
    if module.is_empty() {
        return Vec::new();
    }

    let mut modules = vec![module.clone()];
    if let Some(stripped) = module.strip_prefix("src.") {
        modules.push(stripped.to_string());
    }
    modules
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_candidate_files_cover_module_package_and_src_layouts() {
        let files = python_candidate_files("pkg.utils");
        for expected in [
            "pkg/utils.py",
            "pkg/utils/__init__.py",
            "pkg/utils.pyi",
            "src/pkg/utils.py",
        ] {
            assert!(files.iter().any(|file| file == expected), "{files:?}");
        }
    }

    #[test]
    fn python_candidate_files_handle_top_level_module() {
        let files = python_candidate_files("a");
        assert!(files.iter().any(|file| file == "a.py"), "{files:?}");
        assert!(
            files.iter().any(|file| file == "a/__init__.py"),
            "{files:?}"
        );
    }
}
