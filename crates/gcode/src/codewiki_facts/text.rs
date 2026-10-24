use crate::commands::grep::{self, GrepOptions};
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
        let ctx = self.context();
        let selectors = crate::visibility::visible_selectors_sql(ctx, "$1", "$2");
        let mut conn = self.read_connection()?;
        let rows = conn.query(
            &format!(
                "SELECT c.file_path, c.line_start::BIGINT AS line_start,
                             c.line_end::BIGINT AS line_end, c.content
                      FROM code_content_chunks c JOIN ({selectors}) visible
                        ON visible.project_id = c.project_id AND visible.file_path = c.file_path
                       AND visible.content_hash = c.content_hash
                      WHERE c.file_path = ANY($3) AND c.chunk_index = 0 ORDER BY c.file_path"
            ),
            &[
                &crate::db::id_param(&ctx.machine_id)?,
                &crate::db::id_param(crate::visibility::scope_project_id(ctx))?,
                &paths,
            ],
        )?;
        rows.iter()
            .map(|row| {
                Ok(LeadingChunkFact {
                    file: FileId::new(row.try_get::<_, String>("file_path")?),
                    content: row.try_get("content")?,
                    line_start: usize::try_from(row.try_get::<_, i64>("line_start")?)?,
                    line_end: usize::try_from(row.try_get::<_, i64>("line_end")?)?,
                })
            })
            .collect()
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
            offset: 0,
            token_budget: None,
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
