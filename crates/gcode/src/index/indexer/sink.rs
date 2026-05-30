use postgres::GenericClient;

use crate::index::api;
use crate::models::{CallRelation, ContentChunk, ImportRelation, IndexedFile, Symbol};

pub(super) trait CodeFactSink {
    fn delete_file_facts(&mut self, project_id: &str, file_path: &str) -> anyhow::Result<()>;
    fn upsert_symbols(&mut self, symbols: &[Symbol]) -> anyhow::Result<usize>;
    fn upsert_file(&mut self, file: &IndexedFile) -> anyhow::Result<()>;
    fn upsert_imports(
        &mut self,
        project_id: &str,
        file_path: &str,
        imports: &[ImportRelation],
    ) -> anyhow::Result<usize>;
    fn upsert_calls(
        &mut self,
        project_id: &str,
        file_path: &str,
        calls: &[CallRelation],
    ) -> anyhow::Result<usize>;
    fn upsert_content_chunks(&mut self, chunks: &[ContentChunk]) -> anyhow::Result<usize>;
}

pub(super) struct PostgresCodeFactSink<'a, C> {
    conn: &'a mut C,
}

impl<'a, C> PostgresCodeFactSink<'a, C> {
    pub(super) fn new(conn: &'a mut C) -> Self {
        Self { conn }
    }
}

impl<C> CodeFactSink for PostgresCodeFactSink<'_, C>
where
    C: GenericClient,
{
    fn delete_file_facts(&mut self, project_id: &str, file_path: &str) -> anyhow::Result<()> {
        api::delete_file_facts(self.conn, project_id, file_path)
    }

    fn upsert_symbols(&mut self, symbols: &[Symbol]) -> anyhow::Result<usize> {
        api::upsert_symbols(self.conn, symbols)
    }

    fn upsert_file(&mut self, file: &IndexedFile) -> anyhow::Result<()> {
        api::upsert_file(self.conn, file)
    }

    fn upsert_imports(
        &mut self,
        project_id: &str,
        file_path: &str,
        imports: &[ImportRelation],
    ) -> anyhow::Result<usize> {
        api::upsert_imports(self.conn, project_id, file_path, imports)
    }

    fn upsert_calls(
        &mut self,
        project_id: &str,
        file_path: &str,
        calls: &[CallRelation],
    ) -> anyhow::Result<usize> {
        api::upsert_calls(self.conn, project_id, file_path, calls)
    }

    fn upsert_content_chunks(&mut self, chunks: &[ContentChunk]) -> anyhow::Result<usize> {
        api::upsert_content_chunks(self.conn, chunks)
    }
}
