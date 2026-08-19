use std::collections::BTreeMap;

use crate::commands::grep::{self, GrepOptions};
use crate::config::ProjectIndexScope;
use crate::output::Format;

use super::{CodewikiFacts, FileId, ScopeSelector};

/// First indexed content chunk for a file.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LeadingChunkFact {
    pub file: FileId,
    pub content: String,
    pub line_start: usize,
    pub line_end: usize,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GrepQuery {
    pub pattern: String,
    pub scope: ScopeSelector,
    pub fixed_strings: bool,
    pub ignore_case: bool,
    pub context: Option<usize>,
    pub before_context: Option<usize>,
    pub after_context: Option<usize>,
    pub limit: usize,
}

impl GrepQuery {
    pub fn new(pattern: impl Into<String>, scope: ScopeSelector) -> Self {
        Self {
            pattern: pattern.into(),
            scope,
            fixed_strings: false,
            ignore_case: false,
            context: None,
            before_context: None,
            after_context: None,
            limit: 100,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GrepSpanFact {
    pub start: usize,
    pub end: usize,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GrepContextLineFact {
    pub line: usize,
    pub text: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GrepHit {
    pub path: String,
    pub line: usize,
    pub text: String,
    pub spans: Vec<GrepSpanFact>,
    pub before: Vec<GrepContextLineFact>,
    pub after: Vec<GrepContextLineFact>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GrepOutcome {
    pub scanned_chunks: usize,
    pub matched_lines: usize,
    pub truncated: bool,
    pub hits: Vec<GrepHit>,
}

impl CodewikiFacts {
    pub fn leading_chunks(&self, files: &[FileId]) -> anyhow::Result<Vec<LeadingChunkFact>> {
        if files.is_empty() {
            return Ok(Vec::new());
        }
        let paths = files
            .iter()
            .map(|file| file.as_str().to_string())
            .collect::<Vec<_>>();
        let project_ids = match &self.context().index_scope {
            ProjectIndexScope::Single => vec![self.context().project_id.clone()],
            ProjectIndexScope::Overlay {
                overlay_project_id,
                parent_project_id,
                ..
            } => vec![overlay_project_id.clone(), parent_project_id.clone()],
        };
        let mut chunks = BTreeMap::new();
        let mut conn = self.read_connection()?;
        for project_id in project_ids {
            let project_id = crate::db::id_param(&project_id)?;
            for row in conn.query(
                "SELECT file_path, line_start::BIGINT AS line_start, \
                        line_end::BIGINT AS line_end, content \
                 FROM code_content_chunks \
                 WHERE project_id = $1 AND file_path = ANY($2) AND chunk_index = 0",
                &[&project_id, &paths],
            )? {
                let path: String = row.get("file_path");
                chunks
                    .entry(path.clone())
                    .or_insert_with(|| LeadingChunkFact {
                        file: FileId::new(path),
                        content: row.get("content"),
                        line_start: usize::try_from(row.get::<_, i64>("line_start")).unwrap_or(0),
                        line_end: usize::try_from(row.get::<_, i64>("line_end")).unwrap_or(0),
                    });
            }
        }
        Ok(chunks.into_values().collect())
    }

    pub fn grep(&self, pattern: &str, scope: &ScopeSelector) -> anyhow::Result<GrepOutcome> {
        self.grep_with(&GrepQuery::new(pattern, scope.clone()))
    }

    pub fn grep_with(&self, query: &GrepQuery) -> anyhow::Result<GrepOutcome> {
        let paths = query.scope.normalized(self);
        let options = GrepOptions {
            pattern: &query.pattern,
            paths: &paths,
            globs: &[],
            fixed_strings: query.fixed_strings,
            ignore_case: query.ignore_case,
            word: false,
            context: query.context,
            before_context: query.before_context,
            after_context: query.after_context,
            max_count: Some(query.limit),
            files_with_matches: false,
            format: Format::Text,
        };
        let mut conn = self.read_connection()?;
        let result = grep::grep_repo(self.context(), &mut conn, &options)?;
        Ok(GrepOutcome {
            scanned_chunks: result.scanned_chunks,
            matched_lines: result.matched_lines,
            truncated: result.truncated,
            hits: result
                .matches
                .into_iter()
                .map(|item| GrepHit {
                    path: item.path,
                    line: item.line,
                    text: item.text,
                    spans: item
                        .spans
                        .into_iter()
                        .map(|span| GrepSpanFact {
                            start: span.start,
                            end: span.end,
                        })
                        .collect(),
                    before: item
                        .before
                        .into_iter()
                        .map(|line| GrepContextLineFact {
                            line: line.line,
                            text: line.text,
                        })
                        .collect(),
                    after: item
                        .after
                        .into_iter()
                        .map(|line| GrepContextLineFact {
                            line: line.line,
                            text: line.text,
                        })
                        .collect(),
                })
                .collect(),
        })
    }
}
