use std::path::{Component, Path, PathBuf};

use postgres::Client;

use crate::config::Context;
use crate::db;
use crate::visibility;

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ProjectMatch {
    pub id: String,
    pub root_path: String,
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
}
