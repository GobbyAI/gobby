use std::path::Path;

use serde::Serialize;

use crate::WikiError;
use crate::scope::ResolvedScope;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VaultPaths {
    pub directories: &'static [&'static str],
    pub files: &'static [&'static str],
}

const DIRECTORIES: &[&str] = &[
    "raw",
    "raw/assets",
    "wiki",
    "wiki/sources",
    "wiki/concepts",
    "wiki/topics",
    "inbox",
    "outputs",
    "meta",
    "meta/health",
    ".gwiki",
];

const FILES: &[&str] = &["raw/INDEX.md", "_index.md", "log.md"];

pub fn required_paths() -> VaultPaths {
    VaultPaths {
        directories: DIRECTORIES,
        files: FILES,
    }
}

pub fn initialize(scope: &ResolvedScope) -> Result<(), WikiError> {
    let root = scope.root();
    for directory in DIRECTORIES {
        create_dir(root.join(directory).as_path())?;
    }

    ensure_file(root.join("raw/INDEX.md").as_path(), "# Raw Sources\n\n")?;
    ensure_file(root.join("_index.md").as_path(), "# Wiki Index\n\n")?;
    ensure_file(root.join("log.md").as_path(), "# Log\n\n")?;
    let identity = scope.identity();
    let root_path = root.display().to_string();
    let scope_json = serde_json::to_string_pretty(&ScopeFile {
        identity: &identity,
        root: &root_path,
    })
    .map_err(|error| WikiError::Json {
        action: "serialize scope file",
        path: Some(root.join(".gwiki/scope.json")),
        source: error.to_string(),
    })?;
    ensure_file(
        root.join(".gwiki/scope.json").as_path(),
        format!("{scope_json}\n").as_str(),
    )
}

#[derive(Serialize)]
struct ScopeFile<'a> {
    identity: &'a str,
    root: &'a str,
}

fn create_dir(path: &Path) -> Result<(), WikiError> {
    std::fs::create_dir_all(path).map_err(|error| WikiError::Io {
        action: "create directory",
        path: Some(path.to_path_buf()),
        source: error.to_string(),
    })
}

fn ensure_file(path: &Path, contents: &str) -> Result<(), WikiError> {
    if path.exists() {
        return Ok(());
    }

    if let Some(parent) = path.parent() {
        create_dir(parent)?;
    }
    std::fs::write(path, contents).map_err(|error| WikiError::Io {
        action: "write file",
        path: Some(path.to_path_buf()),
        source: error.to_string(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn vault_shape_lists_required_paths() {
        let paths = required_paths();

        assert!(paths.directories.contains(&"raw/assets"));
        assert!(paths.directories.contains(&"wiki/sources"));
        assert!(paths.directories.contains(&"wiki/concepts"));
        assert!(paths.directories.contains(&"wiki/topics"));
        assert!(paths.directories.contains(&"outputs"));
        assert!(paths.directories.contains(&"meta/health"));
        assert!(paths.files.contains(&"raw/INDEX.md"));
        assert!(paths.files.contains(&"_index.md"));
        assert!(paths.files.contains(&"log.md"));
    }
}
