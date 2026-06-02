use std::io::Write;
use std::path::Path;

use serde::Serialize;

use crate::WikiError;
use crate::scope::ResolvedScope;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VaultPaths {
    pub directories: &'static [&'static str],
    pub files: Vec<&'static str>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CreatedVaultPaths {
    pub directories: Vec<String>,
    pub files: Vec<String>,
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

pub const DEFAULT_FILES: &[(&str, &str)] = &[
    ("raw/INDEX.md", "# Raw Sources\n\n"),
    ("_index.md", "# Wiki Index\n\n"),
    ("log.md", "# Log\n\n"),
];

pub fn required_paths() -> VaultPaths {
    VaultPaths {
        directories: DIRECTORIES,
        files: DEFAULT_FILES.iter().map(|(path, _)| *path).collect(),
    }
}

pub fn initialize(scope: &ResolvedScope) -> Result<CreatedVaultPaths, WikiError> {
    let root = scope.root();
    let mut created = CreatedVaultPaths {
        directories: Vec::new(),
        files: Vec::new(),
    };
    for directory in DIRECTORIES {
        let path = root.join(directory);
        if !path.exists() {
            created.directories.push((*directory).to_string());
        }
        create_dir(path.as_path())?;
    }

    for (path, contents) in DEFAULT_FILES {
        if ensure_file(root.join(path).as_path(), contents)? {
            created.files.push((*path).to_string());
        }
    }
    let identity = scope.identity();
    let root_path = root.display().to_string();
    let scope_file = root.join(".gwiki/scope.json");
    let scope_json = serde_json::to_string_pretty(&ScopeFile {
        identity: &identity,
        root: &root_path,
    })
    .map_err(|error| WikiError::Json {
        action: "serialize scope file",
        path: Some(scope_file.clone()),
        source: error,
    })?;
    let scope_file_created = !scope_file.exists();
    write_file(scope_file.as_path(), format!("{scope_json}\n").as_str())?;
    if scope_file_created {
        created.files.push(".gwiki/scope.json".to_string());
    }
    Ok(created)
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
        source: error,
    })
}

fn ensure_file(path: &Path, contents: &str) -> Result<bool, WikiError> {
    if let Some(parent) = path.parent() {
        create_dir(parent)?;
    }
    match std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
    {
        Ok(mut file) => {
            if let Err(source) = file.write_all(contents.as_bytes()) {
                let _ = std::fs::remove_file(path);
                return Err(WikiError::Io {
                    action: "write file",
                    path: Some(path.to_path_buf()),
                    source,
                });
            }
            Ok(true)
        }
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => Ok(false),
        Err(source) => Err(WikiError::Io {
            action: "create file",
            path: Some(path.to_path_buf()),
            source,
        }),
    }
}

fn write_file(path: &Path, contents: &str) -> Result<(), WikiError> {
    if let Some(parent) = path.parent() {
        create_dir(parent)?;
    }
    std::fs::write(path, contents).map_err(|error| WikiError::Io {
        action: "write file",
        path: Some(path.to_path_buf()),
        source: error,
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

    #[test]
    fn default_files_drive_required_paths_and_contents() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path().join("wiki");
        let scope = ResolvedScope::topic(
            "rust".to_string(),
            root.clone(),
            temp.path().join("wikis.json"),
        );

        initialize(&scope).expect("initialize");
        let required = required_paths();

        assert_eq!(
            required.files,
            DEFAULT_FILES
                .iter()
                .map(|(path, _)| *path)
                .collect::<Vec<_>>()
        );
        for (path, contents) in DEFAULT_FILES {
            assert_eq!(
                std::fs::read_to_string(root.join(path)).expect("read default file"),
                *contents
            );
        }
    }

    #[test]
    fn initialize_overwrites_scope_file() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path().join("wiki");
        let scope = ResolvedScope::topic(
            "rust".to_string(),
            root.clone(),
            temp.path().join("wikis.json"),
        );
        initialize(&scope).expect("initialize once");
        let scope_file = root.join(".gwiki/scope.json");
        std::fs::write(&scope_file, "stale").expect("write stale scope");

        let created = initialize(&scope).expect("initialize twice");

        let contents = std::fs::read_to_string(scope_file).expect("read scope");
        assert!(contents.contains("topic:rust"));
        assert!(!contents.contains("stale"));
        assert!(!created.files.contains(&".gwiki/scope.json".to_string()));
    }
}
