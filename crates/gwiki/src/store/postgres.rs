use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde_json::json;

use super::helpers::{
    clip_link_field, display_path, document_kind_name, ingestion_status,
    platform_path_from_display, rollback_chunk_replacement, rollback_link_replacement, scoped_id,
    scoped_text_id, validate_chunk_paths, validate_link_paths,
};
use super::link_kind;
use super::{
    StoreError, WikiChunk, WikiDocument, WikiIndexStore, WikiIngestion, WikiLink, WikiSource,
    WikiStoreScope,
};

#[derive(Clone)]
struct DocumentMeta {
    id: String,
    source_kind: String,
    content_hash: String,
}

pub struct PostgresWikiStore<'a> {
    conn: &'a mut ::postgres::Client,
    scope: WikiStoreScope,
    documents: BTreeMap<PathBuf, DocumentMeta>,
}

impl<'a> PostgresWikiStore<'a> {
    pub fn new(conn: &'a mut ::postgres::Client, scope: WikiStoreScope) -> Self {
        Self {
            conn,
            scope,
            documents: BTreeMap::new(),
        }
    }

    fn scope_params(&self) -> (String, String, Option<String>, Option<String>) {
        (
            self.scope.scope_kind().to_string(),
            self.scope.scope_id().to_string(),
            self.scope.project_id(),
            self.scope.topic_name(),
        )
    }

    fn document_meta(&mut self, path: &Path) -> Result<DocumentMeta, StoreError> {
        if let Some(meta) = self.documents.get(path) {
            return Ok(meta.clone());
        }

        let path_string = display_path(path);
        let row = self.conn.query_opt(
            "SELECT id, source_kind, content_hash
             FROM gwiki_documents
             WHERE scope_kind = $1 AND scope_id = $2 AND path = $3",
            &[
                &self.scope.scope_kind(),
                &self.scope.scope_id(),
                &path_string,
            ],
        )?;
        let row = row.ok_or_else(|| StoreError::InvalidData {
            field: "document",
            message: format!("missing indexed document for {}", path.display()),
        })?;
        let meta = DocumentMeta {
            id: row.get("id"),
            source_kind: row.get("source_kind"),
            content_hash: row.get("content_hash"),
        };
        self.documents.insert(path.to_path_buf(), meta.clone());
        Ok(meta)
    }
}

impl WikiIndexStore for PostgresWikiStore<'_> {
    fn indexed_hashes(&mut self) -> Result<BTreeMap<PathBuf, String>, StoreError> {
        let rows = self.conn.query(
            "SELECT path, content_hash
             FROM gwiki_documents
             WHERE scope_kind = $1 AND scope_id = $2",
            &[&self.scope.scope_kind(), &self.scope.scope_id()],
        )?;
        Ok(rows
            .into_iter()
            .map(|row| {
                (
                    platform_path_from_display(&row.get::<_, String>("path")),
                    row.get("content_hash"),
                )
            })
            .collect())
    }

    fn upsert_document(&mut self, document: WikiDocument) -> Result<(), StoreError> {
        let id = scoped_id("document", &self.scope, &document.path, None);
        let path = display_path(&document.path);
        let source_kind = document_kind_name(document.kind);
        // serde_json::Value params map natively to JSONB; string params would fail
        // ToSql type checks against jsonb columns (parameter serialization error).
        let provenance = json!({ "source_path": path });
        let frontmatter = json!({});
        let (scope_kind, scope_id, project_id, topic_name) = self.scope_params();

        self.conn.execute(
            "INSERT INTO gwiki_documents (
                id, scope_kind, scope_id, project_id, topic_name, path, title, source_kind,
                content_hash, frontmatter, provenance, body, indexed_at, updated_at
             )
             VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8,
                $9, $10, $11, $12, NOW(), NOW()
             )
             ON CONFLICT (scope_kind, scope_id, path)
             DO UPDATE SET
                id = EXCLUDED.id,
                project_id = EXCLUDED.project_id,
                topic_name = EXCLUDED.topic_name,
                title = EXCLUDED.title,
                source_kind = EXCLUDED.source_kind,
                content_hash = EXCLUDED.content_hash,
                frontmatter = EXCLUDED.frontmatter,
                provenance = EXCLUDED.provenance,
                body = EXCLUDED.body,
                indexed_at = NOW(),
                updated_at = NOW()",
            &[
                &id,
                &scope_kind,
                &scope_id,
                &project_id,
                &topic_name,
                &path,
                &document.title,
                &source_kind,
                &document.content_hash,
                &frontmatter,
                &provenance,
                &document.body,
            ],
        )?;
        self.documents.insert(
            document.path,
            DocumentMeta {
                id,
                source_kind: source_kind.to_string(),
                content_hash: document.content_hash,
            },
        );
        Ok(())
    }

    fn replace_chunks(&mut self, path: &Path, chunks: Vec<WikiChunk>) -> Result<(), StoreError> {
        validate_chunk_paths(path, &chunks)?;
        let document = self.document_meta(path)?;
        let path_string = display_path(path);
        let scope = self.scope.clone();
        let chunks = chunks
            .into_iter()
            .map(|chunk| {
                let chunk_index =
                    i32::try_from(chunk.chunk_index).map_err(|_| StoreError::InvalidData {
                        field: "chunk_index",
                        message: format!(
                            "{} is too large for PostgreSQL INTEGER",
                            chunk.chunk_index
                        ),
                    })?;
                Ok((chunk, chunk_index))
            })
            .collect::<Result<Vec<_>, StoreError>>()?;
        let mut tx = self.conn.transaction()?;
        if let Err(error) = tx.execute(
            "DELETE FROM gwiki_chunks WHERE scope_kind = $1 AND scope_id = $2 AND path = $3",
            &[&scope.scope_kind(), &scope.scope_id(), &path_string],
        ) {
            let error = StoreError::from(error);
            rollback_chunk_replacement(tx, &path_string);
            return Err(error);
        }
        if chunks.is_empty() {
            tx.commit()?;
            return Ok(());
        }

        for (chunk, chunk_index) in chunks {
            let chunk_path = display_path(&chunk.path);
            let id = scoped_id(
                "chunk",
                &scope,
                &chunk.path,
                Some(&chunk.chunk_index.to_string()),
            );
            let heading_path = chunk
                .heading
                .as_ref()
                .map(|heading| vec![heading.clone()])
                .unwrap_or_default();
            let provenance = json!({
                "source_path": chunk_path,
                "byte_start": chunk.byte_start,
                "byte_end": chunk.byte_end,
                "heading": chunk.heading,
            });
            let frontmatter = json!({});
            let (scope_kind, scope_id, project_id, topic_name) = (
                scope.scope_kind().to_string(),
                scope.scope_id().to_string(),
                scope.project_id(),
                scope.topic_name(),
            );

            if let Err(error) = tx.execute(
                "INSERT INTO gwiki_chunks (
                    id, document_id, scope_kind, scope_id, project_id, topic_name, path,
                    chunk_index, source_kind, content_hash, frontmatter, provenance,
                    heading_path, content, created_at
                 )
                 VALUES (
                    $1, $2, $3, $4, $5, $6, $7,
                    $8, $9, $10, $11, $12,
                    $13, $14, NOW()
                 )",
                &[
                    &id,
                    &document.id,
                    &scope_kind,
                    &scope_id,
                    &project_id,
                    &topic_name,
                    &chunk_path,
                    &chunk_index,
                    &document.source_kind,
                    &document.content_hash,
                    &frontmatter,
                    &provenance,
                    &heading_path,
                    &chunk.content,
                ],
            ) {
                let error = StoreError::from(error);
                rollback_chunk_replacement(tx, &path_string);
                return Err(error);
            }
        }

        tx.commit()?;
        Ok(())
    }

    fn replace_links(&mut self, path: &Path, links: Vec<WikiLink>) -> Result<(), StoreError> {
        validate_link_paths(path, &links)?;
        let path_string = display_path(path);
        let scope = self.scope.clone();
        let mut tx = self.conn.transaction()?;
        if let Err(error) = tx.execute(
            "DELETE FROM gwiki_links WHERE scope_kind = $1 AND scope_id = $2 AND path = $3",
            &[&scope.scope_kind(), &scope.scope_id(), &path_string],
        ) {
            let error = StoreError::from(error);
            rollback_link_replacement(tx, &path_string);
            return Err(error);
        }

        for link in links {
            // Clip unbounded fields so the gwiki_links_scope_idx composite row
            // stays under Postgres's 8191-byte btree limit; the id is derived
            // from the clipped values to keep insert/conflict keys consistent.
            let target_path = clip_link_field(&link.target);
            let link_text = clip_link_field(link.alias.as_deref().unwrap_or(link.target.as_str()));
            let link_kind = link_kind(&link.target);
            let id = scoped_text_id(
                "link",
                &scope,
                &link.path,
                &[&target_path, &link_text, link_kind],
            );
            let path = display_path(&link.path);
            let provenance = json!({
                "byte_start": link.byte_start,
                "byte_end": link.byte_end,
                "alias": link.alias,
            });
            let (scope_kind, scope_id, project_id, topic_name) = (
                scope.scope_kind().to_string(),
                scope.scope_id().to_string(),
                scope.project_id(),
                scope.topic_name(),
            );

            if let Err(error) = tx.execute(
                "INSERT INTO gwiki_links (
                    id, scope_kind, scope_id, project_id, topic_name, path,
                    target_path, link_text, link_kind, provenance, created_at
                 )
                 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW())
             ON CONFLICT (scope_kind, scope_id, path, target_path, link_text, link_kind)
             DO UPDATE SET
                    id = EXCLUDED.id,
                    project_id = EXCLUDED.project_id,
                    topic_name = EXCLUDED.topic_name,
                    provenance = EXCLUDED.provenance",
                &[
                    &id,
                    &scope_kind,
                    &scope_id,
                    &project_id,
                    &topic_name,
                    &path,
                    &target_path,
                    &link_text,
                    &link_kind,
                    &provenance,
                ],
            ) {
                let error = StoreError::from(error);
                rollback_link_replacement(tx, &path_string);
                return Err(error);
            }
        }

        tx.commit().map_err(StoreError::from)
    }

    fn upsert_source(&mut self, source: WikiSource) -> Result<(), StoreError> {
        let id = scoped_id("source", &self.scope, &source.document_path, None);
        let path = display_path(&source.path);
        let document_path = display_path(&source.document_path);
        let source_kind = document_kind_name(source.kind);
        let provenance = json!({
            "source_path": &path,
            "document_path": &document_path,
        });
        let frontmatter = json!({});
        let (scope_kind, scope_id, project_id, topic_name) = self.scope_params();

        self.conn.execute(
            "INSERT INTO gwiki_sources (
				id, scope_kind, scope_id, project_id, topic_name, path, document_path, source_kind,
				content_hash, frontmatter, provenance, captured_at
			 )
			 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW())
			 ON CONFLICT (scope_kind, scope_id, document_path)
			 DO UPDATE SET
				id = EXCLUDED.id,
				project_id = EXCLUDED.project_id,
				topic_name = EXCLUDED.topic_name,
				path = EXCLUDED.path,
				source_kind = EXCLUDED.source_kind,
				content_hash = EXCLUDED.content_hash,
				frontmatter = EXCLUDED.frontmatter,
                provenance = EXCLUDED.provenance,
                captured_at = NOW()",
            &[
                &id,
                &scope_kind,
                &scope_id,
                &project_id,
                &topic_name,
                &path,
                &document_path,
                &source_kind,
                &source.content_hash,
                &frontmatter,
                &provenance,
            ],
        )?;
        Ok(())
    }

    fn record_ingestion(&mut self, ingestion: WikiIngestion) -> Result<(), StoreError> {
        let content_hash = ingestion.content_hash.clone();
        let status = ingestion_status(ingestion.event);
        let id = scoped_text_id("ingestion", &self.scope, &ingestion.path, &[status]);
        let path = display_path(&ingestion.path);
        let source_kind = self
            .documents
            .get(&ingestion.path)
            .map(|document| document.source_kind.as_str())
            .unwrap_or("unknown");
        let provenance = json!({ "event": status });
        let frontmatter = json!({});
        let (scope_kind, scope_id, project_id, topic_name) = self.scope_params();

        self.conn.execute(
            "INSERT INTO gwiki_ingestions (
                id, scope_kind, scope_id, project_id, topic_name, path, source_kind,
                content_hash, frontmatter, provenance, status, ingested_at
             )
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW())
             ON CONFLICT (id)
             DO UPDATE SET
                project_id = EXCLUDED.project_id,
                topic_name = EXCLUDED.topic_name,
                source_kind = EXCLUDED.source_kind,
                content_hash = EXCLUDED.content_hash,
                frontmatter = EXCLUDED.frontmatter,
                provenance = EXCLUDED.provenance,
                status = EXCLUDED.status,
                ingested_at = NOW()",
            &[
                &id,
                &scope_kind,
                &scope_id,
                &project_id,
                &topic_name,
                &path,
                &source_kind,
                &content_hash,
                &frontmatter,
                &provenance,
                &status,
            ],
        )?;
        Ok(())
    }

    fn record_file_hash(
        &mut self,
        _path: PathBuf,
        _content_hash: String,
    ) -> Result<(), StoreError> {
        // PostgreSQL stores file hashes on gwiki_documents via upsert_document.
        Ok(())
    }

    fn delete_derived_rows(&mut self, path: &Path) -> Result<(), StoreError> {
        let path = display_path(path);
        let scope_kind = self.scope.scope_kind().to_string();
        let scope_id = self.scope.scope_id().to_string();
        let mut tx = self.conn.transaction()?;
        let params: [&(dyn ::postgres::types::ToSql + Sync); 3] = [&scope_kind, &scope_id, &path];
        tx.execute(
            "DELETE FROM gwiki_chunks WHERE scope_kind = $1 AND scope_id = $2 AND path = $3",
            &params,
        )?;
        tx.execute(
            "DELETE FROM gwiki_links WHERE scope_kind = $1 AND scope_id = $2 AND path = $3",
            &params,
        )?;
        tx.execute(
			"DELETE FROM gwiki_sources WHERE scope_kind = $1 AND scope_id = $2 AND document_path = $3",
			&params,
		)?;
        tx.execute(
            "DELETE FROM gwiki_documents WHERE scope_kind = $1 AND scope_id = $2 AND path = $3",
            &params,
        )?;
        tx.commit()?;
        self.documents.remove(&platform_path_from_display(&path));
        Ok(())
    }
}
