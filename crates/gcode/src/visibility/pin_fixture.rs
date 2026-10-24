use std::path::PathBuf;

use crate::config::{Context, ProjectIndexScope};
use crate::db;
use crate::index::api;
use crate::models::{ContentChunk, ImportRelation, IndexedFile, Symbol};

pub struct PinFixture {
    pub conn: postgres::Client,
    pub ctx: Context,
    pub source: String,
    projects: Vec<String>,
    _workspace: tempfile::TempDir,
}

impl PinFixture {
    pub fn new() -> anyhow::Result<Self> {
        let database_url = crate::test_env::postgres_test_database_url("pinned visibility");
        let mut conn = db::connect_readwrite(&database_url)?;
        let workspace = tempfile::tempdir()?;
        let machine_id = gobby_core::machine::read_local_machine_id()?;
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)?
            .as_nanos();
        let id = |label| {
            uuid::Uuid::new_v5(
                &crate::models::CODE_INDEX_UUID_NAMESPACE,
                format!("pin-visibility-{label}-{}-{nonce}", std::process::id()).as_bytes(),
            )
            .to_string()
        };
        let source = id("source");
        let overlay = id("overlay");
        let parent = id("parent");
        for project in [&source, &overlay, &parent] {
            api::upsert_project_seed(
                &mut conn,
                &machine_id,
                project,
                std::path::Path::new("/tmp/pin-test"),
                api::IndexWriteMode::Overlay,
            )?;
        }
        let ctx = Context {
            database_url,
            project_root: workspace.path().to_path_buf(),
            project_id: overlay.clone(),
            quiet: true,
            falkordb: None,
            qdrant: None,
            embedding: None,
            code_vectors: crate::config::CodeVectorSettings::default(),
            machine_id,
            runtime_config_capture_degraded: false,
            indexing: gobby_core::config::IndexingConfig::default(),
            daemon_url: None,
            grant_ai: None,
            index_scope: ProjectIndexScope::Overlay {
                overlay_project_id: overlay.clone(),
                overlay_root: PathBuf::from("/tmp/pin-test"),
                parent_project_id: parent.clone(),
                parent_root: PathBuf::from("/tmp/pin-parent"),
            },
        };
        Ok(Self {
            conn,
            ctx,
            source: source.clone(),
            projects: vec![overlay, parent, source],
            _workspace: workspace,
        })
    }

    pub fn parent(&self) -> String {
        match &self.ctx.index_scope {
            ProjectIndexScope::Overlay {
                parent_project_id, ..
            } => parent_project_id.clone(),
            ProjectIndexScope::Single => unreachable!(),
        }
    }

    pub fn version(
        &mut self,
        project: &str,
        path: &str,
        hash: &str,
        name: &str,
    ) -> anyhow::Result<Symbol> {
        let tombstone = hash == crate::visibility::TOMBSTONE_HASH;
        let child_path = self.ctx.project_root.join(path);
        if tombstone {
            if child_path.exists() {
                std::fs::remove_file(&child_path)?;
            }
        } else if project == self.ctx.project_id || !child_path.exists() {
            std::fs::write(&child_path, format!("scopewindowneedle {name}"))?;
        }
        let file = IndexedFile {
            id: IndexedFile::make_id(project, path, hash),
            project_id: project.into(),
            file_path: path.into(),
            language: if tombstone {
                crate::visibility::TOMBSTONE_LANGUAGE
            } else {
                "rust"
            }
            .into(),
            content_hash: hash.into(),
            symbol_count: usize::from(!tombstone),
            byte_size: 1,
            indexed_at: String::new(),
        };
        api::upsert_file(&mut self.conn, &file)?;
        api::file_state::upsert_file_state(
            &mut self.conn,
            &self.ctx.machine_id,
            &file,
            &self.ctx.project_root,
            api::IndexWriteMode::Overlay,
        )?;
        let symbol = Symbol {
            id: Symbol::make_id(project, path, hash, name, "function", 0),
            project_id: project.into(),
            file_path: path.into(),
            name: name.into(),
            qualified_name: name.into(),
            kind: "function".into(),
            language: "rust".into(),
            byte_start: 0,
            byte_end: 1,
            line_start: 1,
            line_end: 1,
            signature: Some(name.into()),
            docstring: None,
            parent_symbol_id: None,
            file_content_hash: hash.into(),
            content_hash: hash.into(),
            summary: Some("scopewindowneedle".into()),
            created_at: String::new(),
            updated_at: String::new(),
        };
        if !tombstone {
            api::upsert_symbols(&mut self.conn, std::slice::from_ref(&symbol))?;
            api::upsert_content_chunks(
                &mut self.conn,
                &[ContentChunk {
                    id: ContentChunk::make_id(project, path, hash, 0),
                    project_id: project.into(),
                    file_path: path.into(),
                    content_hash: hash.into(),
                    chunk_index: 0,
                    line_start: 1,
                    line_end: 1,
                    content: format!("scopewindowneedle {name}"),
                    language: "rust".into(),
                    created_at: String::new(),
                }],
            )?;
            api::upsert_imports(
                &mut self.conn,
                project,
                path,
                hash,
                &[ImportRelation {
                    file_path: path.into(),
                    module_name: name.into(),
                }],
            )?;
        }
        Ok(symbol)
    }

    pub fn pin(&mut self, source: &str, path: &str, hash: &str) -> anyhow::Result<()> {
        self.conn.execute(
            "INSERT INTO code_overlay_pins
             (machine_id, overlay_project_id, file_path, source_project_id, content_hash)
             VALUES ($1, $2, $3, $4, $5)",
            &[
                &db::id_param(&self.ctx.machine_id)?,
                &db::id_param(&self.ctx.project_id)?,
                &path,
                &db::id_param(source)?,
                &hash,
            ],
        )?;
        Ok(())
    }
}

impl Drop for PinFixture {
    fn drop(&mut self) {
        for project in &self.projects {
            let id = db::id_param(project).expect("fixture project");
            for sql in [
                "DELETE FROM code_overlay_pins WHERE overlay_project_id = $1 OR source_project_id = $1",
                "DELETE FROM code_indexed_file_states WHERE project_id = $1",
                "DELETE FROM code_imports WHERE project_id = $1",
                "DELETE FROM code_symbols WHERE project_id = $1",
                "DELETE FROM code_content_chunks WHERE project_id = $1",
                "DELETE FROM code_indexed_files WHERE project_id = $1",
                "DELETE FROM code_indexed_project_states WHERE project_id = $1",
                "DELETE FROM code_indexed_projects WHERE id = $1",
            ] {
                let _ = self.conn.execute(sql, &[&id]);
            }
        }
    }
}
