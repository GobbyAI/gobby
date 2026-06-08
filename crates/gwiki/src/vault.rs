use std::io::Write;
use std::path::Path;

use serde::Serialize;

use crate::WikiError;
use crate::scope::ResolvedScope;

/// Unified Obsidian vault layout shared by gwiki and gcode codewiki.
///
/// `code/` contains generated code documentation, `knowledge/` contains
/// synthesized wiki pages, and `_meta/` contains shared generation metadata.
/// Legacy `wiki/**` paths remain indexable for existing vaults.
pub const CODE_ROOT: &str = "code";
pub const KNOWLEDGE_ROOT: &str = "knowledge";
pub const SHARED_META_ROOT: &str = "_meta";

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
    CODE_ROOT,
    KNOWLEDGE_ROOT,
    "knowledge/sources",
    "knowledge/concepts",
    "knowledge/topics",
    SHARED_META_ROOT,
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
    ("knowledge/INDEX.md", "# Knowledge\n\n"),
    ("code/INDEX.md", "# Code\n\n"),
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
    write_scope_file_atomically(scope_file.as_path(), format!("{scope_json}\n").as_bytes())?;
    if scope_file_created {
        created.files.push(".gwiki/scope.json".to_string());
    }
    Ok(created)
}

pub fn cleanup_created(root: &Path, created: &CreatedVaultPaths) -> Result<(), WikiError> {
    for file in &created.files {
        let path = root.join(file);
        match std::fs::remove_file(&path) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(source) => {
                return Err(WikiError::Io {
                    action: "remove initialized file",
                    path: Some(path),
                    source,
                });
            }
        }
    }

    for directory in created.directories.iter().rev() {
        let path = root.join(directory);
        match std::fs::remove_dir(&path) {
            Ok(()) => {}
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::NotFound | std::io::ErrorKind::DirectoryNotEmpty
                ) => {}
            Err(source) => {
                return Err(WikiError::Io {
                    action: "remove initialized directory",
                    path: Some(path),
                    source,
                });
            }
        }
    }

    Ok(())
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

fn write_scope_file_atomically(path: &Path, contents: &[u8]) -> Result<(), WikiError> {
    if let Some(parent) = path.parent() {
        create_dir(parent)?;
    }
    let temp_path = temp_sibling_path(path);
    let mut file = std::fs::File::create(&temp_path).map_err(|error| WikiError::Io {
        action: "create scope file temp file",
        path: Some(temp_path.clone()),
        source: error,
    })?;
    if let Err(error) = file.write_all(contents) {
        let _ = std::fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action: "write scope file temp file",
            path: Some(temp_path),
            source: error,
        });
    }
    if let Err(error) = file.sync_all() {
        let _ = std::fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action: "sync scope file temp file",
            path: Some(temp_path),
            source: error,
        });
    }
    drop(file);
    if let Err(error) = std::fs::rename(&temp_path, path) {
        let _ = std::fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action: "replace scope file",
            path: Some(path.to_path_buf()),
            source: error,
        });
    }
    sync_parent_dir(path)
}

fn temp_sibling_path(path: &Path) -> std::path::PathBuf {
    let file_name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("scope.json");
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or_default();
    path.with_file_name(format!(".{file_name}.{}.{nanos}.tmp", std::process::id()))
}

fn sync_parent_dir(path: &Path) -> Result<(), WikiError> {
    #[cfg(not(unix))]
    {
        let _ = path;
        Ok(())
    }
    #[cfg(unix)]
    {
        let Some(parent) = path.parent() else {
            return Ok(());
        };
        std::fs::File::open(parent)
            .and_then(|dir| dir.sync_all())
            .map_err(|error| WikiError::Io {
                action: "sync scope file directory",
                path: Some(parent.to_path_buf()),
                source: error,
            })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn vault_shape_lists_required_paths() {
        let paths = required_paths();

        assert!(paths.directories.contains(&"raw/assets"));
        assert!(paths.directories.contains(&"code"));
        assert!(paths.directories.contains(&"knowledge"));
        assert!(paths.directories.contains(&"_meta"));
        assert!(paths.directories.contains(&"wiki/sources"));
        assert!(paths.directories.contains(&"wiki/concepts"));
        assert!(paths.directories.contains(&"wiki/topics"));
        assert!(paths.directories.contains(&"outputs"));
        assert!(paths.directories.contains(&"meta/health"));
        assert!(paths.files.contains(&"raw/INDEX.md"));
        assert!(paths.files.contains(&"knowledge/INDEX.md"));
        assert!(paths.files.contains(&"code/INDEX.md"));
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

    #[test]
    fn cleanup_created_removes_only_created_vault_paths() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path().join("wiki");
        let scope = ResolvedScope::topic(
            "rust".to_string(),
            root.clone(),
            temp.path().join("wikis.json"),
        );
        let created = initialize(&scope).expect("initialize");

        cleanup_created(&root, &created).expect("cleanup created paths");

        for file in created.files {
            assert!(!root.join(file).exists());
        }
        for directory in created.directories {
            assert!(!root.join(directory).exists());
        }
        assert!(root.exists());
    }
}
