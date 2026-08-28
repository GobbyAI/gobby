use std::path::{Component, Path, PathBuf};

use postgres::Client;

use crate::cli_error::CliError;
use crate::config::Context;
use crate::db;
use crate::visibility;

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ProjectMatch {
    pub id: String,
    pub root_path: String,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum ScopedPathInput<'a> {
    ExactFile(&'a str),
    Filter(&'a str),
    Glob(&'a str),
}

impl<'a> ScopedPathInput<'a> {
    fn value(self) -> &'a str {
        match self {
            Self::ExactFile(value) | Self::Filter(value) | Self::Glob(value) => value,
        }
    }
}

#[derive(Debug)]
struct ScopeRoot {
    lexical: PathBuf,
    canonical: PathBuf,
}

pub(crate) fn resolve_path_input(
    ctx: &Context,
    cwd: &Path,
    input: ScopedPathInput<'_>,
) -> Result<String, CliError> {
    let value = input.value();
    let path = Path::new(value);
    let base = if path.is_absolute() {
        PathBuf::new()
    } else if starts_from_cwd(path) {
        cwd.to_path_buf()
    } else {
        ctx.project_root.clone()
    };
    let candidate = normalize_absolute_path(&base.join(path)).ok_or_else(|| {
        CliError::invalid_path_scope(value, discover_project_root(&base.join(path)).as_deref())
    })?;
    let roots = scope_roots(ctx, cwd);
    let existing = longest_existing_ancestor(&candidate);
    let canonical_existing = existing.as_ref().and_then(|path| path.canonicalize().ok());

    let lexical_root = roots
        .iter()
        .find(|root| candidate.starts_with(&root.lexical));
    let canonical_root = canonical_existing.as_ref().and_then(|existing| {
        roots
            .iter()
            .find(|root| existing.starts_with(&root.canonical))
    });

    if canonical_existing.is_some() && canonical_root.is_none() {
        return Err(CliError::invalid_path_scope(
            value,
            canonical_existing
                .as_deref()
                .and_then(discover_project_root)
                .as_deref(),
        ));
    }

    let relative = if let Some(root) = lexical_root {
        candidate
            .strip_prefix(&root.lexical)
            .ok()
            .map(Path::to_path_buf)
    } else if let (Some(root), Some(existing), Some(canonical_existing)) = (
        canonical_root,
        existing.as_ref(),
        canonical_existing.as_ref(),
    ) {
        let canonical_relative = canonical_existing.strip_prefix(&root.canonical).ok();
        let suffix = candidate.strip_prefix(existing).ok();
        canonical_relative.map(|relative| relative.join(suffix.unwrap_or(Path::new(""))))
    } else {
        None
    };

    relative
        .map(|path| clean_relative_path(&path))
        .ok_or_else(|| {
            CliError::invalid_path_scope(value, discover_project_root(&candidate).as_deref())
        })
}

fn scope_roots(ctx: &Context, cwd: &Path) -> Vec<ScopeRoot> {
    let mut paths = vec![ctx.project_root.as_path()];
    if let crate::config::ProjectIndexScope::Overlay {
        overlay_root,
        parent_root,
        ..
    } = &ctx.index_scope
    {
        paths.push(overlay_root);
        paths.push(parent_root);
    }

    let mut roots = paths
        .into_iter()
        .filter_map(|path| {
            let absolute = if path.is_absolute() {
                path.to_path_buf()
            } else {
                cwd.join(path)
            };
            let lexical = normalize_absolute_path(&absolute)?;
            let canonical = lexical.canonicalize().unwrap_or_else(|_| lexical.clone());
            Some(ScopeRoot { lexical, canonical })
        })
        .collect::<Vec<_>>();
    roots.sort_by_key(|root| std::cmp::Reverse(root.lexical.components().count()));
    roots.dedup_by(|left, right| left.lexical == right.lexical);
    roots
}

fn starts_from_cwd(path: &Path) -> bool {
    matches!(
        path.components().next(),
        Some(Component::CurDir | Component::ParentDir)
    )
}

fn normalize_absolute_path(path: &Path) -> Option<PathBuf> {
    if !path.is_absolute() {
        return None;
    }
    let mut normalized = PathBuf::new();
    for component in path.components() {
        match component {
            Component::Prefix(_) | Component::RootDir | Component::Normal(_) => {
                normalized.push(component.as_os_str());
            }
            Component::CurDir => {}
            Component::ParentDir if !normalized.pop() => return None,
            Component::ParentDir => {}
        }
    }
    Some(normalized)
}

fn longest_existing_ancestor(path: &Path) -> Option<PathBuf> {
    let mut candidate = path.to_path_buf();
    loop {
        if candidate.exists() {
            return Some(candidate);
        }
        if !candidate.pop() {
            return None;
        }
    }
}

fn discover_project_root(path: &Path) -> Option<PathBuf> {
    let anchor = longest_existing_ancestor(path)?;
    let root = crate::config::detect_project_root_from(&anchor).ok()?;
    let has_marker = root.join(".gobby/project.json").is_file()
        || root.join(".gobby/gcode.json").is_file()
        || root.join(".git").exists()
        || root.join(".hg").exists();
    has_marker.then_some(root)
}

pub(crate) fn normalize_file_arg(ctx: &Context, file: &str) -> String {
    let path = Path::new(file);
    if path.is_absolute() {
        if let Ok(rel) = path.strip_prefix(&ctx.project_root) {
            return clean_relative_path(rel);
        }
        if let (Ok(abs), Ok(root)) = (path.canonicalize(), ctx.project_root.canonicalize())
            && let Ok(rel) = abs.strip_prefix(root)
        {
            return clean_relative_path(rel);
        }
    }
    clean_relative_path(path)
}

pub(crate) fn path_exists_in_current_project(ctx: &Context, file_path: &str) -> bool {
    if path_exists_under_root(&ctx.project_root, file_path) {
        return true;
    }

    if let crate::config::ProjectIndexScope::Overlay {
        overlay_root,
        parent_root,
        ..
    } = &ctx.index_scope
    {
        path_exists_under_root(overlay_root, file_path)
            || path_exists_under_root(parent_root, file_path)
    } else {
        false
    }
}

fn path_exists_under_root(root: &Path, file_path: &str) -> bool {
    let path = root.join(file_path);
    if !path.exists() {
        return false;
    }

    let Ok(root) = root.canonicalize() else {
        return false;
    };
    let Ok(abs) = path.canonicalize() else {
        return false;
    };
    abs.starts_with(root)
}

pub(crate) fn current_indexed_path_is_valid(
    conn: &mut Client,
    ctx: &Context,
    file_path: &str,
) -> bool {
    visibility::indexed_file_exists(conn, ctx, file_path)
        && path_exists_in_current_project(ctx, file_path)
}

pub(crate) fn other_project_for_path(
    conn: &mut Client,
    ctx: &Context,
    file_path: &str,
) -> Option<ProjectMatch> {
    let machine_id = db::id_param(&gobby_core::machine::read_local_machine_id().ok()?).ok()?;
    if let Some(project) =
        indexed_project_for_file_path(conn, &machine_id, &ctx.project_id, file_path)
    {
        return Some(project);
    }

    let current_root = ctx.project_root.canonicalize().ok();
    let rows = conn
        .query(
            "SELECT project_id AS id, root_path FROM code_indexed_project_states
             WHERE machine_id = $1 AND project_id != $2 AND root_path != ''
             ORDER BY root_path",
            &[&machine_id, &db::id_param(&ctx.project_id).ok()?],
        )
        .ok()?;

    for row in rows {
        let project = ProjectMatch {
            id: db::id_string(&row, "id").ok()?,
            root_path: row.try_get("root_path").ok()?,
        };
        let root = PathBuf::from(&project.root_path);
        if current_root.as_ref().is_some_and(|current| {
            root.canonicalize()
                .map(|candidate| candidate == *current)
                .unwrap_or(false)
        }) {
            continue;
        }
        if root.join(file_path).exists() {
            return Some(project);
        }
    }

    None
}

fn indexed_project_for_file_path(
    conn: &mut Client,
    machine_id: &uuid::Uuid,
    current_project_id: &str,
    file_path: &str,
) -> Option<ProjectMatch> {
    conn.query_opt(
        "SELECT s.project_id AS id, p.root_path
             FROM code_indexed_file_states s
             JOIN code_indexed_project_states p
               ON p.machine_id = s.machine_id AND p.project_id = s.project_id
             WHERE s.machine_id = $1 AND s.file_path = $2 AND s.project_id != $3
             ORDER BY p.root_path
             LIMIT 1",
        &[
            &machine_id,
            &file_path,
            &db::id_param(current_project_id).ok()?,
        ],
    )
    .ok()
    .flatten()
    .and_then(|row| {
        Some(ProjectMatch {
            id: db::id_string(&row, "id").ok()?,
            root_path: row.try_get("root_path").ok()?,
        })
    })
}

fn clean_relative_path(path: &Path) -> String {
    let mut out = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::Normal(part) => out.push(part),
            Component::ParentDir => out.push(".."),
            Component::Prefix(_) | Component::RootDir => {}
        }
    }
    crate::index::normalize_storage_path(&out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Context;

    fn context_for(root: PathBuf) -> Context {
        Context {
            database_url: "postgresql://localhost/gobby-test".to_string(),
            project_root: root,
            project_id: "current".to_string(),
            quiet: false,
            falkordb: None,
            qdrant: None,
            embedding: None,
            code_vectors: crate::config::CodeVectorSettings::default(),
            runtime_config_capture_degraded: false,
            indexing: gobby_core::config::IndexingConfig::default(),
            daemon_url: None,
            grant_ai: None,
            index_scope: crate::config::ProjectIndexScope::Single,
        }
    }

    #[test]
    fn normalizes_absolute_path_inside_project() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let src = tmp.path().join("src");
        std::fs::create_dir_all(&src).expect("create src");
        let file = src.join("main.rs");
        std::fs::write(&file, "fn main() {}").expect("write file");
        let ctx = context_for(tmp.path().to_path_buf());

        assert_eq!(
            normalize_file_arg(&ctx, &file.to_string_lossy()),
            "src/main.rs"
        );
    }

    #[test]
    fn clean_relative_path_drops_absolute_root_components() {
        assert_eq!(
            clean_relative_path(Path::new("/tmp/project/src/lib.rs")),
            "tmp/project/src/lib.rs"
        );
    }

    #[test]
    fn path_exists_accepts_overlay_parent_files() {
        let overlay = tempfile::tempdir().expect("overlay tempdir");
        let parent = tempfile::tempdir().expect("parent tempdir");
        std::fs::create_dir_all(parent.path().join("src")).expect("create parent src");
        std::fs::write(parent.path().join("src/lib.rs"), "pub fn parent() {}\n")
            .expect("write parent file");
        let mut ctx = context_for(overlay.path().to_path_buf());
        ctx.index_scope = crate::config::ProjectIndexScope::Overlay {
            overlay_project_id: "overlay".to_string(),
            overlay_root: overlay.path().to_path_buf(),
            parent_project_id: "parent".to_string(),
            parent_root: parent.path().to_path_buf(),
        };

        assert!(path_exists_in_current_project(&ctx, "src/lib.rs"));
    }

    #[test]
    fn resolves_project_cwd_absolute_and_nonexistent_inputs() {
        let project = tempfile::tempdir().expect("project tempdir");
        let root = project.path();
        std::fs::create_dir_all(root.join("src/nested")).expect("create project directories");
        std::fs::write(root.join("src/lib.rs"), "pub fn current() {}\n")
            .expect("write project file");
        let ctx = context_for(root.to_path_buf());
        let cwd = root.join("src/nested");

        assert_eq!(
            resolve_path_input(&ctx, &cwd, ScopedPathInput::ExactFile("src/lib.rs"))
                .expect("resolve project-relative file"),
            "src/lib.rs"
        );
        assert_eq!(
            resolve_path_input(&ctx, &cwd, ScopedPathInput::ExactFile("../new.rs"))
                .expect("resolve cwd-relative file"),
            "src/new.rs"
        );
        assert_eq!(
            resolve_path_input(
                &ctx,
                &cwd,
                ScopedPathInput::ExactFile(root.join("src/lib.rs").to_string_lossy().as_ref()),
            )
            .expect("resolve absolute file"),
            "src/lib.rs"
        );
        assert_eq!(
            resolve_path_input(
                &ctx,
                &cwd,
                ScopedPathInput::ExactFile("generated/missing.rs"),
            )
            .expect("resolve nonexistent in-scope file"),
            "generated/missing.rs"
        );
    }

    #[test]
    fn preserves_filter_and_glob_syntax() {
        let project = tempfile::tempdir().expect("project tempdir");
        std::fs::create_dir_all(project.path().join("src/nested"))
            .expect("create project directories");
        let ctx = context_for(project.path().to_path_buf());

        assert_eq!(
            resolve_path_input(
                &ctx,
                project.path(),
                ScopedPathInput::Filter("src/**/[a-z]*.rs"),
            )
            .expect("resolve filter"),
            "src/**/[a-z]*.rs"
        );
        assert_eq!(
            resolve_path_input(
                &ctx,
                &project.path().join("src"),
                ScopedPathInput::Glob("./nested/{one,two}.rs"),
            )
            .expect("resolve glob"),
            "src/nested/{one,two}.rs"
        );
    }

    #[test]
    fn maps_overlay_parent_paths_to_storage_relative_paths() {
        let overlay = tempfile::tempdir().expect("overlay tempdir");
        let parent = tempfile::tempdir().expect("parent tempdir");
        std::fs::create_dir_all(parent.path().join("src")).expect("create parent src");
        std::fs::write(parent.path().join("src/lib.rs"), "pub fn parent() {}\n")
            .expect("write parent file");
        let mut ctx = context_for(overlay.path().to_path_buf());
        ctx.index_scope = crate::config::ProjectIndexScope::Overlay {
            overlay_project_id: "overlay".to_string(),
            overlay_root: overlay.path().to_path_buf(),
            parent_project_id: "parent".to_string(),
            parent_root: parent.path().to_path_buf(),
        };

        assert_eq!(
            resolve_path_input(
                &ctx,
                overlay.path(),
                ScopedPathInput::ExactFile(
                    parent.path().join("src/lib.rs").to_string_lossy().as_ref(),
                ),
            )
            .expect("resolve parent file"),
            "src/lib.rs"
        );
    }

    #[test]
    fn rejects_lexical_and_cross_project_escapes() {
        let sandbox = tempfile::tempdir().expect("sandbox tempdir");
        let root = sandbox.path().join("current");
        let other = sandbox.path().join("other");
        std::fs::create_dir_all(root.join("src")).expect("create current project");
        std::fs::create_dir_all(other.join(".git")).expect("create other project marker");
        std::fs::write(other.join("other.rs"), "pub fn other() {}\n")
            .expect("write other project file");
        let ctx = context_for(root.clone());

        let lexical = resolve_path_input(
            &ctx,
            &root.join("src"),
            ScopedPathInput::ExactFile("../../outside.rs"),
        )
        .expect_err("reject lexical escape");
        assert_eq!(lexical.code, "invalid_path_scope");

        let cross_project = resolve_path_input(
            &ctx,
            &root,
            ScopedPathInput::ExactFile(other.join("other.rs").to_string_lossy().as_ref()),
        )
        .expect_err("reject cross-project path");
        assert_eq!(cross_project.code, "invalid_path_scope");
        assert!(
            cross_project
                .recovery
                .as_deref()
                .is_some_and(|value| value.contains("--project") && value.contains("other"))
        );
    }

    #[cfg(unix)]
    #[test]
    fn rejects_symlink_escape() {
        use std::os::unix::fs::symlink;

        let project = tempfile::tempdir().expect("project tempdir");
        let outside = tempfile::tempdir().expect("outside tempdir");
        std::fs::write(outside.path().join("secret.rs"), "secret\n").expect("write outside file");
        symlink(outside.path(), project.path().join("linked")).expect("create symlink");
        let ctx = context_for(project.path().to_path_buf());

        let error = resolve_path_input(
            &ctx,
            project.path(),
            ScopedPathInput::ExactFile("linked/secret.rs"),
        )
        .expect_err("reject symlink escape");
        assert_eq!(error.code, "invalid_path_scope");
    }
}
