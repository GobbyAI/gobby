use std::path::{Path, PathBuf};

use postgres::GenericClient;

use crate::index::api;
use crate::models::{
    CallRelation, ContentChunk, ImportRelation, IndexedFile, InheritanceRelation, Symbol,
};

pub(super) trait CodeFactSink {
    fn delete_file_non_symbol_facts(
        &mut self,
        project_id: &str,
        file_path: &str,
        content_hash: &str,
    ) -> anyhow::Result<()>;
    fn delete_stale_file_symbols(
        &mut self,
        project_id: &str,
        file_path: &str,
        content_hash: &str,
        current_symbol_ids: &[String],
    ) -> anyhow::Result<usize>;
    fn upsert_symbols(&mut self, symbols: &[Symbol]) -> anyhow::Result<usize>;
    fn upsert_file(&mut self, file: &IndexedFile) -> anyhow::Result<()>;
    fn upsert_imports(
        &mut self,
        project_id: &str,
        file_path: &str,
        content_hash: &str,
        imports: &[ImportRelation],
    ) -> anyhow::Result<usize>;
    fn upsert_calls(
        &mut self,
        project_id: &str,
        file_path: &str,
        content_hash: &str,
        calls: &[CallRelation],
    ) -> anyhow::Result<usize>;
    fn upsert_inheritance(
        &mut self,
        project_id: &str,
        file_path: &str,
        content_hash: &str,
        inheritance: &[InheritanceRelation],
    ) -> anyhow::Result<usize>;
    fn upsert_content_chunks(&mut self, chunks: &[ContentChunk]) -> anyhow::Result<usize>;
}

pub(super) struct PostgresCodeFactSink<'a, C> {
    conn: &'a mut C,
    machine_id: String,
    root_path: PathBuf,
    mode: api::IndexWriteMode,
}

impl<'a, C> PostgresCodeFactSink<'a, C> {
    pub(super) fn new(
        conn: &'a mut C,
        project_id: &str,
        root_path: &Path,
        mode: api::IndexWriteMode,
    ) -> anyhow::Result<Self>
    where
        C: GenericClient,
    {
        let machine_id = gobby_core::machine::read_local_machine_id()?;
        api::upsert_project_seed(conn, &machine_id, project_id, root_path, mode)?;
        Ok(Self {
            conn,
            machine_id,
            root_path: root_path.to_path_buf(),
            mode,
        })
    }
}

impl<C> CodeFactSink for PostgresCodeFactSink<'_, C>
where
    C: GenericClient,
{
    fn delete_file_non_symbol_facts(
        &mut self,
        project_id: &str,
        file_path: &str,
        content_hash: &str,
    ) -> anyhow::Result<()> {
        api::delete_content_version_non_symbol_facts(self.conn, project_id, file_path, content_hash)
    }

    fn delete_stale_file_symbols(
        &mut self,
        project_id: &str,
        file_path: &str,
        content_hash: &str,
        current_symbol_ids: &[String],
    ) -> anyhow::Result<usize> {
        api::delete_stale_file_symbols(
            self.conn,
            project_id,
            file_path,
            content_hash,
            current_symbol_ids,
        )
    }

    fn upsert_symbols(&mut self, symbols: &[Symbol]) -> anyhow::Result<usize> {
        api::upsert_symbols(self.conn, symbols)
    }

    fn upsert_file(&mut self, file: &IndexedFile) -> anyhow::Result<()> {
        api::upsert_file(self.conn, file)?;
        api::upsert_file_state(
            self.conn,
            &self.machine_id,
            file,
            &self.root_path,
            self.mode,
        )
    }

    fn upsert_imports(
        &mut self,
        project_id: &str,
        file_path: &str,
        content_hash: &str,
        imports: &[ImportRelation],
    ) -> anyhow::Result<usize> {
        api::upsert_imports(self.conn, project_id, file_path, content_hash, imports)
    }

    fn upsert_calls(
        &mut self,
        project_id: &str,
        file_path: &str,
        content_hash: &str,
        calls: &[CallRelation],
    ) -> anyhow::Result<usize> {
        api::upsert_calls(self.conn, project_id, file_path, content_hash, calls)
    }

    fn upsert_inheritance(
        &mut self,
        project_id: &str,
        file_path: &str,
        content_hash: &str,
        inheritance: &[InheritanceRelation],
    ) -> anyhow::Result<usize> {
        api::upsert_inheritance(self.conn, project_id, file_path, content_hash, inheritance)
    }

    fn upsert_content_chunks(&mut self, chunks: &[ContentChunk]) -> anyhow::Result<usize> {
        api::upsert_content_chunks(self.conn, chunks)
    }
}
