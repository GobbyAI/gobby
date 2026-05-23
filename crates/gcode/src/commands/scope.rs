use std::path::{Component, Path, PathBuf};

use postgres::Client;

use crate::config::Context;

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
    let path = ctx.project_root.join(file_path);
    if !path.exists() {
        return false;
    }

    let Ok(root) = ctx.project_root.canonicalize() else {
        return false;
    };
    let Ok(abs) = path.canonicalize() else {
        return false;
    };
    abs.starts_with(root)
}

pub(crate) fn indexed_file_exists(conn: &mut Client, project_id: &str, file_path: &str) -> bool {
    conn.query_one(
        "SELECT EXISTS(
            SELECT 1 FROM code_indexed_files
            WHERE project_id = $1 AND file_path = $2
        )",
        &[&project_id, &file_path],
    )
    .and_then(|row| row.try_get::<_, bool>(0))
    .unwrap_or(false)
}

pub(crate) fn content_chunks_exist(conn: &mut Client, project_id: &str, file_path: &str) -> bool {
    conn.query_one(
        "SELECT EXISTS(
            SELECT 1 FROM code_content_chunks
            WHERE project_id = $1 AND file_path = $2
        )",
        &[&project_id, &file_path],
    )
    .and_then(|row| row.try_get::<_, bool>(0))
    .unwrap_or(false)
}

pub(crate) fn current_indexed_path_is_valid(
    conn: &mut Client,
    ctx: &Context,
    file_path: &str,
) -> bool {
    indexed_file_exists(conn, &ctx.project_id, file_path)
        && path_exists_in_current_project(ctx, file_path)
}

pub(crate) fn other_project_for_path(
    conn: &mut Client,
    ctx: &Context,
    file_path: &str,
) -> Option<ProjectMatch> {
    if let Some(project) = indexed_project_for_file_path(conn, &ctx.project_id, file_path) {
        return Some(project);
    }

    let current_root = ctx.project_root.canonicalize().ok();
    let rows = conn
        .query(
            "SELECT id, root_path FROM code_indexed_projects
             WHERE id != $1 AND root_path != ''
             ORDER BY root_path",
            &[&ctx.project_id],
        )
        .ok()?;

    for row in rows {
        let project = ProjectMatch {
            id: row.try_get("id").ok()?,
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
    current_project_id: &str,
    file_path: &str,
) -> Option<ProjectMatch> {
    conn.query_opt(
        "SELECT p.id, p.root_path
             FROM code_indexed_files f
             JOIN code_indexed_projects p ON p.id = f.project_id
             WHERE f.file_path = $1 AND f.project_id != $2
             ORDER BY p.root_path
             LIMIT 1",
        &[&file_path, &current_project_id],
    )
    .ok()
    .flatten()
    .and_then(|row| {
        Some(ProjectMatch {
            id: row.try_get("id").ok()?,
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
    out.to_string_lossy().replace('\\', "/")
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
            daemon_url: None,
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
}
