use std::io::Write;
use std::path::Path;

use serde::Serialize;

use crate::WikiError;
use crate::scope::ResolvedScope;

/// Unified Obsidian vault layout shared by gwiki commands, including `gwiki code`.
///
/// `code/` contains generated code documentation, `knowledge/` contains
/// synthesized wiki pages, and `_meta/` contains shared generation metadata.
pub const CODE_ROOT: &str = "code";
pub const KNOWLEDGE_ROOT: &str = "knowledge";
pub const SHARED_META_ROOT: &str = "_meta";

/// Per-vault control/state dir and scope-file name. Owned by
/// `gobby_core::vault` so gcode's walker and gwiki address the same layout;
/// re-exported here for gwiki-internal callers.
pub use gobby_core::vault::{SCOPE_FILE, STATE_ROOT};

#[derive(Debug, Clone, PartialEq, Eq)]
#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
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
    "inbox",
    "outputs",
    "meta",
    "meta/health",
    STATE_ROOT,
];

pub const DEFAULT_FILES: &[(&str, &str)] = &[
    ("raw/INDEX.md", "# Raw Sources\n\n"),
    ("knowledge/INDEX.md", "# Knowledge\n\n"),
    ("code/INDEX.md", "# Code\n\n"),
    ("_index.md", "# Wiki Index\n\n"),
    ("log.md", "# Log\n\n"),
    (AI_README_FILE, AI_README_TEMPLATE),
];

/// Static agent-navigation readme at the vault root. Written once by
/// `gwiki init`/`setup` scaffolding and restored by catalog regeneration only
/// when missing (#17730) — user edits are preserved.
pub const AI_README_FILE: &str = "ai-readme.md";
pub const AI_README_TEMPLATE: &str = "# AI Agent Guide\n\n\
Navigation guide for AI agents working this vault. Written by `gwiki init`; \
restored by catalog regeneration only when missing, so local edits are \
preserved.\n\n\
## Layout\n\n\
- `_index.md` — vault overview: totals, top concepts, recent work.\n\
- `knowledge/` — synthesized knowledge (`concepts/`, `topics/`, `sources/` digests); `knowledge/INDEX.md` lists everything.\n\
- `code/` — generated code documentation; `code/INDEX.md` groups handbook, concepts, modules, and files.\n\
- `recaps/` — daily session recap pages.\n\
- `raw/` — captured source material backing `knowledge/sources/`.\n\
- Content folders carry a `_context.md` with a deterministic listing of their pages and subfolders.\n\n\
## Machine-readable exports (`outputs/`)\n\n\
- `outputs/pages/<page>.json` — per-page metadata sibling: frontmatter, outbound links, lifecycle, confidence, audit claim classification (`gwiki export pages`).\n\
- `outputs/graph.jsonld`, `outputs/llms.txt`, `outputs/llms-full.txt` — schema.org document graph and llms.txt indexes (`gwiki graph`).\n\n\
The daemon refreshes these exports on a schedule.\n\n\
## Trust signals\n\n\
- Frontmatter `lifecycle`: `draft | reviewed | verified | stale | archived`. Archived pages and quarantined candidates (`candidate: true`) are excluded from agent surfaces.\n\
- Page confidence (0-100, derived): cited-source credibility, freshness half-life, and backlinks; surfaced by `gwiki health`, `gwiki trust`, and per-page JSON.\n\
- Claim classification: `EXTRACTED` (directly cited), `INFERRED` (page-level grounding), `AMBIGUOUS` (flagged uncertainty).\n\n\
## Query\n\n\
- `gwiki search \"<term>\"` — retrieval over the vault.\n\
- `gwiki read <page>` — read a page with metadata.\n\
- `gwiki trust` — trust status of the search, graph, freshness, and audit surfaces.\n";

#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
pub fn required_paths() -> VaultPaths {
    VaultPaths {
        directories: DIRECTORIES,
        files: DEFAULT_FILES.iter().map(|(path, _)| *path).collect(),
    }
}

pub fn initialize(scope: &ResolvedScope) -> Result<CreatedVaultPaths, WikiError> {
    if let Some(owner) = owner_for_scope(scope)? {
        return initialize_under_owner(&owner, scope);
    }
    initialize_unmanaged(scope)
}

fn owner_for_scope(scope: &ResolvedScope) -> Result<Option<crate::owner_fs::OwnerRoot>, WikiError> {
    let view =
        gobby_core::bootstrap::read_files_home_view().map_err(|error| WikiError::Config {
            detail: error.to_string(),
        })?;
    let Some(files_home) = view.files_home else {
        return Ok(None);
    };
    if !scope.root().starts_with(&files_home) {
        return Ok(None);
    }
    Ok(Some(crate::owner_fs::OwnerRoot::open(&files_home)?))
}

fn initialize_under_owner(
    owner: &crate::owner_fs::OwnerRoot,
    scope: &ResolvedScope,
) -> Result<CreatedVaultPaths, WikiError> {
    let relative_root = scope
        .root()
        .strip_prefix(owner.path())
        .map_err(|_| WikiError::Config {
            detail: format!(
                "scope root {} is not under files_home {}",
                scope.root().display(),
                owner.path().display()
            ),
        })?;
    let mut created = CreatedVaultPaths {
        directories: Vec::new(),
        files: Vec::new(),
    };
    for directory in DIRECTORIES {
        let relative = relative_root.join(directory);
        if !owner.path().join(&relative).exists() {
            created.directories.push((*directory).to_string());
        }
        owner.create_dir_all(&relative)?;
    }
    for (path, contents) in DEFAULT_FILES {
        if owner.write_file_if_absent(&relative_root.join(path), contents)? {
            created.files.push((*path).to_string());
        }
    }
    let identity = scope.identity();
    let root_path = scope.root().display().to_string();
    let scope_relative = relative_root.join(STATE_ROOT).join(SCOPE_FILE);
    let scope_file = owner.path().join(&scope_relative);
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
    owner.replace_file(&scope_relative, format!("{scope_json}\n").as_bytes())?;
    if scope_file_created {
        created.files.push(format!("{STATE_ROOT}/{SCOPE_FILE}"));
    }
    Ok(created)
}

fn initialize_unmanaged(scope: &ResolvedScope) -> Result<CreatedVaultPaths, WikiError> {
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
    let scope_file = root.join(STATE_ROOT).join(SCOPE_FILE);
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
        created.files.push(format!("{STATE_ROOT}/{SCOPE_FILE}"));
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

pub(crate) fn ensure_file(path: &Path, contents: &str) -> Result<bool, WikiError> {
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
        .unwrap_or(SCOPE_FILE);
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
        assert!(paths.directories.contains(&"knowledge/sources"));
        assert!(paths.directories.contains(&"knowledge/concepts"));
        assert!(paths.directories.contains(&"knowledge/topics"));
        assert!(paths.directories.contains(&"outputs"));
        assert!(paths.directories.contains(&"meta/health"));
        assert!(paths.files.contains(&"raw/INDEX.md"));
        assert!(paths.files.contains(&"knowledge/INDEX.md"));
        assert!(paths.files.contains(&"code/INDEX.md"));
        assert!(paths.files.contains(&"_index.md"));
        assert!(paths.files.contains(&"log.md"));
    }

    fn isolated_home(temp: &tempfile::TempDir) -> crate::support::test_env::EnvGuard {
        let home = temp.path().join("gobby-home");
        std::fs::create_dir_all(&home).expect("gobby home");
        crate::support::test_env::EnvGuard::set("GOBBY_HOME", home.as_os_str())
    }

    #[test]
    #[serial_test::serial]
    fn default_files_drive_required_paths_and_contents() {
        let temp = tempfile::tempdir().expect("tempdir");
        let _env = isolated_home(&temp);
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
    fn export_health_ai_readme_mentions_scheduled_refresh() {
        assert!(AI_README_TEMPLATE.contains("daemon refreshes these exports on a schedule"));
    }

    #[test]
    #[serial_test::serial]
    fn initialize_overwrites_scope_file() {
        let temp = tempfile::tempdir().expect("tempdir");
        let _env = isolated_home(&temp);
        let root = temp.path().join("wiki");
        let scope = ResolvedScope::topic(
            "rust".to_string(),
            root.clone(),
            temp.path().join("wikis.json"),
        );
        initialize(&scope).expect("initialize once");
        let scope_file = root.join(STATE_ROOT).join(SCOPE_FILE);
        std::fs::write(&scope_file, "stale").expect("write stale scope");

        let created = initialize(&scope).expect("initialize twice");

        let contents = std::fs::read_to_string(scope_file).expect("read scope");
        assert!(contents.contains("topic:rust"));
        assert!(!contents.contains("stale"));
        assert!(
            !created
                .files
                .contains(&format!("{STATE_ROOT}/{SCOPE_FILE}"))
        );
    }

    #[test]
    #[serial_test::serial]
    fn cleanup_created_removes_only_created_vault_paths() {
        let temp = tempfile::tempdir().expect("tempdir");
        let _env = isolated_home(&temp);
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
