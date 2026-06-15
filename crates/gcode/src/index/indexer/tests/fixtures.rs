use crate::config::Context;
use std::path::{Path, PathBuf};

pub(super) fn write_file(root: &Path, rel: &str, contents: &[u8]) {
    let path = root.join(rel);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).expect("create parent");
    }
    std::fs::write(path, contents).expect("write file");
}

pub(super) fn test_context() -> Context {
    Context {
        database_url: "postgresql://localhost/nonexistent".to_string(),
        project_root: PathBuf::from("/project"),
        project_id: "project-1".to_string(),
        quiet: true,
        falkordb: None,
        qdrant: None,
        embedding: None,
        code_vectors: crate::config::CodeVectorSettings { vector_dim: None },
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        index_scope: crate::config::ProjectIndexScope::Single,
    }
}
