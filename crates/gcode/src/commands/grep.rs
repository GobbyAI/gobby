use std::collections::{BTreeMap, BTreeSet};

use postgres::Client;
use postgres::types::ToSql;
use serde::Serialize;

mod grep_matcher;

#[cfg(test)]
#[path = "grep/db_tests.rs"]
mod db_tests;

use crate::commands::scope;
use crate::config::{Context, ProjectIndexScope};
use crate::db;
use crate::output::{self, Format};
use crate::search::fts;
use crate::utils::i64_to_usize;
use crate::visibility;

use grep_matcher::GrepMatcher;

const GREP_SQL_SAFETY_LIMIT: i64 = 100_000;

pub struct GrepOptions<'a> {
    pub pattern: &'a str,
    pub paths: &'a [String],
    pub globs: &'a [String],
    pub fixed_strings: bool,
    pub ignore_case: bool,
    pub word: bool,
    pub context: Option<usize>,
    pub before_context: Option<usize>,
    pub after_context: Option<usize>,
    pub max_count: Option<usize>,
    pub files_with_matches: bool,
    pub format: Format,
}

#[derive(Debug, Clone)]
struct IndexedContentChunk {
    file_path: String,
    line_start: usize,
    content: String,
}

#[derive(Debug)]
struct LoadedIndexedChunks {
    chunks: Vec<IndexedContentChunk>,
    truncated: bool,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub(crate) struct GrepSpan {
    pub start: usize,
    pub end: usize,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub(crate) struct GrepContextLine {
    pub line: usize,
    pub text: String,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub(crate) struct GrepMatch {
    pub path: String,
    pub line: usize,
    pub text: String,
    pub spans: Vec<GrepSpan>,
    pub before: Vec<GrepContextLine>,
    pub after: Vec<GrepContextLine>,
}

#[derive(Debug, Serialize)]
struct GrepResponse {
    project_id: String,
    pattern: String,
    fixed_strings: bool,
    ignore_case: bool,
    word: bool,
    paths: Vec<String>,
    globs: Vec<String>,
    max_count: Option<usize>,
    matched_lines: usize,
    truncated: bool,
    scanned_chunks: usize,
    matches: Vec<GrepMatch>,
    #[serde(skip_serializing_if = "Option::is_none")]
    files: Option<Vec<String>>,
}

#[derive(Debug)]
pub(crate) struct GrepResult {
    pub(crate) scanned_chunks: usize,
    pub(crate) matched_lines: usize,
    pub(crate) truncated: bool,
    pub(crate) matches: Vec<GrepMatch>,
}

pub fn run(ctx: &Context, options: GrepOptions<'_>) -> anyhow::Result<()> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let filters = GrepFilters::new(options.paths, options.globs)?;
    let loaded = load_indexed_chunks(&mut conn, ctx, &filters)?;
    let mut result = grep_chunks_with_filters(&loaded.chunks, &options, &filters)?;
    result.truncated |= loaded.truncated;

    match options.format {
        Format::Json => output::print_json(&grep_response(&ctx.project_id, &options, &result)),
        Format::Text => {
            let text = if options.files_with_matches {
                let (files, _) = matching_files(&result.matches, options.max_count);
                format_matching_files(&files)
            } else {
                format_text_matches(&result.matches)
            };
            if text.is_empty() {
                Ok(())
            } else {
                output::print_text(&text)
            }
        }
    }
}

/// Result-returning grep beneath the CLI print layer, for the CodeWiki tool loop
/// tool executor (#978): loads the indexed content chunks in scope and runs the
/// same matcher [`run`] uses, returning the structured matches instead of
/// printing them. Reuses the existing read-only connection rather than opening
/// its own.
pub(crate) fn grep_repo(
    ctx: &Context,
    conn: &mut Client,
    options: &GrepOptions<'_>,
) -> anyhow::Result<GrepResult> {
    let filters = GrepFilters::new(options.paths, options.globs)?;
    let loaded = load_indexed_chunks(conn, ctx, &filters)?;
    let mut result = grep_chunks_with_filters(&loaded.chunks, options, &filters)?;
    result.truncated |= loaded.truncated;
    Ok(result)
}

fn load_indexed_chunks(
    conn: &mut Client,
    ctx: &Context,
    filters: &GrepFilters,
) -> anyhow::Result<LoadedIndexedChunks> {
    let mut chunks = Vec::new();
    let tombstone_language = visibility::TOMBSTONE_LANGUAGE;
    let Some(machine_id) = visibility::local_machine_uuid_or_invisible() else {
        return Ok(LoadedIndexedChunks {
            chunks,
            truncated: false,
        });
    };
    let rows = match &ctx.index_scope {
        ProjectIndexScope::Single => {
            let project_id = db::id_param(&ctx.project_id)?;
            let mut params: Vec<&(dyn ToSql + Sync)> =
                vec![&project_id, &tombstone_language, &machine_id];
            let mut conditions = vec![
                "c.project_id = $1".to_string(),
                "cf.language != $2".to_string(),
                "fs.machine_id = $3".to_string(),
            ];
            push_grep_sql_prefilters(&mut conditions, &mut params, "c", filters);
            let limit = GREP_SQL_SAFETY_LIMIT + 1;
            let limit_placeholder = format!("${}", params.len() + 1);
            params.push(&limit);
            let sql = format!(
                "SELECT c.file_path,
                        c.line_start::BIGINT AS line_start,
                        c.content
                 FROM code_content_chunks c
                 JOIN code_indexed_file_states fs
                   ON fs.project_id = c.project_id
                  AND fs.file_path = c.file_path
                  AND fs.content_hash = c.content_hash
                 JOIN code_indexed_files cf
                   ON cf.project_id = c.project_id
                  AND cf.file_path = c.file_path
                  AND cf.content_hash = c.content_hash
                 WHERE {}
                 ORDER BY c.file_path ASC, c.line_start ASC, c.chunk_index ASC
                 LIMIT {limit_placeholder}",
                conditions.join(" AND ")
            );
            conn.query(&sql, &params)?
        }
        ProjectIndexScope::Overlay {
            overlay_project_id,
            parent_project_id,
            ..
        } => {
            let overlay_project_id = db::id_param(overlay_project_id)?;
            let parent_project_id = db::id_param(parent_project_id)?;
            let mut params: Vec<&(dyn ToSql + Sync)> = vec![
                &overlay_project_id,
                &parent_project_id,
                &tombstone_language,
                &machine_id,
            ];
            let mut conditions = vec![
                "cf.language != $3".to_string(),
                "fs.machine_id = $4".to_string(),
                "(
                    c.project_id = $1
                    OR (
                        c.project_id = $2
                        AND NOT EXISTS (
                            SELECT 1 FROM code_indexed_file_states shadow
                            WHERE shadow.machine_id = $4
                              AND shadow.project_id = $1
                              AND shadow.file_path = c.file_path
                        )
                    )
                )"
                .to_string(),
            ];
            push_grep_sql_prefilters(&mut conditions, &mut params, "c", filters);
            let limit = GREP_SQL_SAFETY_LIMIT + 1;
            let limit_placeholder = format!("${}", params.len() + 1);
            params.push(&limit);
            let sql = format!(
                "SELECT c.file_path,
                        c.line_start::BIGINT AS line_start,
                        c.content
                 FROM code_content_chunks c
                 JOIN code_indexed_file_states fs
                   ON fs.project_id = c.project_id
                  AND fs.file_path = c.file_path
                  AND fs.content_hash = c.content_hash
                 JOIN code_indexed_files cf
                   ON cf.project_id = c.project_id
                  AND cf.file_path = c.file_path
                  AND cf.content_hash = c.content_hash
                 WHERE {}
                 ORDER BY c.file_path ASC, c.line_start ASC, c.chunk_index ASC
                 LIMIT {limit_placeholder}",
                conditions.join(" AND ")
            );
            conn.query(&sql, &params)?
        }
    };
    let mut valid_paths = BTreeMap::<String, bool>::new();
    let mut truncated = false;
    for row in rows {
        if chunks.len() >= GREP_SQL_SAFETY_LIMIT as usize {
            truncated = true;
            break;
        }
        let file_path: String = row.try_get("file_path")?;
        let is_valid = match valid_paths.get(&file_path) {
            Some(is_valid) => *is_valid,
            None => {
                let is_valid = scope::current_indexed_path_is_valid(conn, ctx, &file_path);
                valid_paths.insert(file_path.clone(), is_valid);
                is_valid
            }
        };
        if !is_valid {
            continue;
        }
        let line_start = i64_to_usize(row.try_get("line_start")?, "line_start")?;
        chunks.push(IndexedContentChunk {
            file_path,
            line_start,
            content: row.try_get("content")?,
        });
    }
    if matches!(&ctx.index_scope, ProjectIndexScope::Overlay { .. }) {
        chunks.sort_by(|a, b| {
            a.file_path
                .cmp(&b.file_path)
                .then_with(|| a.line_start.cmp(&b.line_start))
        });
    }
    Ok(LoadedIndexedChunks { chunks, truncated })
}

fn push_grep_sql_prefilters<'a>(
    conditions: &mut Vec<String>,
    params: &mut Vec<&'a (dyn ToSql + Sync)>,
    alias: &str,
    filters: &'a GrepFilters,
) {
    push_grep_sql_prefix_filter(
        conditions,
        params,
        alias,
        filters.path_sql_prefixes.as_ref(),
    );
    push_grep_sql_prefix_filter(
        conditions,
        params,
        alias,
        filters.glob_sql_prefixes.as_ref(),
    );
}

fn push_grep_sql_prefix_filter<'a>(
    conditions: &mut Vec<String>,
    params: &mut Vec<&'a (dyn ToSql + Sync)>,
    alias: &str,
    prefixes: Option<&'a Vec<String>>,
) {
    let Some(prefixes) = prefixes else {
        return;
    };
    if prefixes.is_empty() {
        return;
    }
    let placeholder = format!("${}", params.len() + 1);
    params.push(prefixes);
    conditions.push(format!(
        "EXISTS (
            SELECT 1 FROM unnest({placeholder}::TEXT[]) AS grep_prefix(value)
            WHERE {alias}.file_path LIKE grep_prefix.value ESCAPE '\\'
        )"
    ));
}

#[cfg(test)]
fn grep_chunks(
    chunks: &[IndexedContentChunk],
    options: &GrepOptions<'_>,
) -> anyhow::Result<GrepResult> {
    let filters = GrepFilters::new(options.paths, options.globs)?;
    grep_chunks_with_filters(chunks, options, &filters)
}

fn grep_chunks_with_filters(
    chunks: &[IndexedContentChunk],
    options: &GrepOptions<'_>,
    filters: &GrepFilters,
) -> anyhow::Result<GrepResult> {
    let matcher = GrepMatcher::new(
        options.pattern,
        options.fixed_strings,
        options.ignore_case,
        options.word,
    )?;
    let (before_context, after_context) = if options.files_with_matches {
        (0, 0)
    } else {
        (
            options.before_context.or(options.context).unwrap_or(0),
            options.after_context.or(options.context).unwrap_or(0),
        )
    };

    let mut scanned_chunks = 0usize;
    let mut matches: BTreeMap<(String, usize), GrepMatch> = BTreeMap::new();

    for chunk in chunks {
        if !filters.matches(&chunk.file_path) {
            continue;
        }
        scanned_chunks += 1;

        for (offset, line_text) in chunk.content.lines().enumerate() {
            let line = chunk.line_start + offset;
            let key = (chunk.file_path.clone(), line);
            if matches.contains_key(&key) {
                continue;
            }

            let spans = matcher.find_spans(line_text);
            if !spans.is_empty() {
                matches.insert(
                    key,
                    GrepMatch {
                        path: chunk.file_path.clone(),
                        line,
                        text: line_text.to_string(),
                        spans,
                        before: Vec::new(),
                        after: Vec::new(),
                    },
                );
            }
        }
    }

    let total_matching_lines = matches.len();
    let max = if options.files_with_matches {
        usize::MAX
    } else {
        options.max_count.unwrap_or(usize::MAX)
    };
    let mut retained = matches.into_values().take(max).collect::<Vec<_>>();
    let needed_context = context_line_numbers(&retained, before_context, after_context);
    let context_lines = collect_context_lines(chunks, filters, &needed_context);
    for item in &mut retained {
        if let Some(lines) = context_lines.get(&item.path) {
            item.before = context_before(lines, item.line, before_context);
            item.after = context_after(lines, item.line, after_context);
        }
    }

    Ok(GrepResult {
        scanned_chunks,
        matched_lines: total_matching_lines,
        truncated: total_matching_lines > retained.len(),
        matches: retained,
    })
}

fn context_line_numbers(
    matches: &[GrepMatch],
    before_context: usize,
    after_context: usize,
) -> BTreeMap<String, BTreeSet<usize>> {
    let mut needed = BTreeMap::<String, BTreeSet<usize>>::new();
    for item in matches {
        let lines = needed.entry(item.path.clone()).or_default();
        if before_context > 0 {
            for line in item.line.saturating_sub(before_context)..item.line {
                lines.insert(line);
            }
        }
        if after_context > 0 {
            let end = item.line.saturating_add(after_context);
            for line in item.line.saturating_add(1)..=end {
                lines.insert(line);
            }
        }
    }
    needed
}

fn collect_context_lines(
    chunks: &[IndexedContentChunk],
    filters: &GrepFilters,
    needed: &BTreeMap<String, BTreeSet<usize>>,
) -> BTreeMap<String, BTreeMap<usize, String>> {
    let mut context_lines = BTreeMap::<String, BTreeMap<usize, String>>::new();
    if needed.is_empty() {
        return context_lines;
    }

    for chunk in chunks {
        if !filters.matches(&chunk.file_path) {
            continue;
        }
        let Some(needed_lines) = needed.get(&chunk.file_path) else {
            continue;
        };
        for (offset, line_text) in chunk.content.lines().enumerate() {
            let line = chunk.line_start + offset;
            if needed_lines.contains(&line) {
                context_lines
                    .entry(chunk.file_path.clone())
                    .or_default()
                    .entry(line)
                    .or_insert_with(|| line_text.to_string());
            }
        }
    }

    context_lines
}

struct GrepFilters {
    paths: Vec<glob::Pattern>,
    globs: Vec<CompiledGlob>,
    path_sql_prefixes: Option<Vec<String>>,
    glob_sql_prefixes: Option<Vec<String>>,
}

impl GrepFilters {
    fn new(paths: &[String], globs: &[String]) -> anyhow::Result<Self> {
        let expanded_paths = fts::expand_paths(paths);
        let path_sql_prefixes = sql_like_prefixes(&expanded_paths);
        let glob_sql_prefixes = sql_like_prefixes(globs);
        Ok(Self {
            paths: fts::compile_patterns(&expanded_paths)?,
            globs: globs
                .iter()
                .map(|glob| CompiledGlob::new(glob))
                .collect::<anyhow::Result<Vec<_>>>()?,
            path_sql_prefixes,
            glob_sql_prefixes,
        })
    }

    fn matches(&self, file_path: &str) -> bool {
        let path_matches =
            self.paths.is_empty() || self.paths.iter().any(|pattern| pattern.matches(file_path));
        let glob_matches =
            self.globs.is_empty() || self.globs.iter().any(|glob| glob.matches(file_path));
        path_matches && glob_matches
    }
}

fn sql_like_prefixes(patterns: &[String]) -> Option<Vec<String>> {
    if patterns.is_empty() {
        return None;
    }
    let mut prefixes = Vec::new();
    for pattern in patterns {
        let prefix = pattern
            .chars()
            .take_while(|ch| !matches!(ch, '*' | '?' | '['))
            .collect::<String>();
        if !prefix.is_empty() {
            prefixes.push(format!("{}%", escape_like_prefix(&prefix)));
        }
    }
    (!prefixes.is_empty()).then_some(prefixes)
}

fn escape_like_prefix(value: &str) -> String {
    let mut escaped = String::with_capacity(value.len());
    for ch in value.chars() {
        if matches!(ch, '%' | '_' | '\\') {
            escaped.push('\\');
        }
        escaped.push(ch);
    }
    escaped
}

struct CompiledGlob {
    raw: String,
    pattern: glob::Pattern,
}

impl CompiledGlob {
    fn new(raw: &str) -> anyhow::Result<Self> {
        Ok(Self {
            raw: raw.to_string(),
            pattern: glob::Pattern::new(raw)
                .map_err(|err| anyhow::anyhow!("invalid grep glob `{raw}`: {err}"))?,
        })
    }

    fn matches(&self, file_path: &str) -> bool {
        // Match ripgrep-style basename globs (`*.rs`) while keeping slash
        // globs (`src/*.rs`) scoped to the full indexed path.
        if self.pattern.matches(file_path) {
            return true;
        }
        if self.raw.contains('/') {
            return false;
        }
        file_path
            .rsplit('/')
            .next()
            .is_some_and(|name| self.pattern.matches(name))
    }
}

fn context_before(
    lines: &BTreeMap<usize, String>,
    line: usize,
    context: usize,
) -> Vec<GrepContextLine> {
    if context == 0 {
        return Vec::new();
    }
    let start = line.saturating_sub(context);
    lines
        .range(start..line)
        .map(|(line, text)| GrepContextLine {
            line: *line,
            text: text.clone(),
        })
        .collect()
}

fn context_after(
    lines: &BTreeMap<usize, String>,
    line: usize,
    context: usize,
) -> Vec<GrepContextLine> {
    if context == 0 {
        return Vec::new();
    }
    let end = line.saturating_add(context);
    lines
        .range((line.saturating_add(1))..=end)
        .map(|(line, text)| GrepContextLine {
            line: *line,
            text: text.clone(),
        })
        .collect()
}

fn matching_files(matches: &[GrepMatch], max_count: Option<usize>) -> (Vec<String>, bool) {
    let mut files: Vec<String> = matches
        .iter()
        .map(|item| item.path.as_str())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .map(str::to_string)
        .collect();
    let total = files.len();
    let limit = max_count.unwrap_or(usize::MAX);
    let truncated = total > limit;
    files.truncate(limit);
    (files, truncated)
}

fn format_matching_files(files: &[String]) -> String {
    files.join("\n")
}

fn grep_response(project_id: &str, options: &GrepOptions<'_>, result: &GrepResult) -> GrepResponse {
    let (matches, files, truncated) = if options.files_with_matches {
        let (files, file_truncated) = matching_files(&result.matches, options.max_count);
        (Vec::new(), Some(files), result.truncated || file_truncated)
    } else {
        (result.matches.clone(), None, result.truncated)
    };
    GrepResponse {
        project_id: project_id.to_string(),
        pattern: options.pattern.to_string(),
        fixed_strings: options.fixed_strings,
        ignore_case: options.ignore_case,
        word: options.word,
        paths: options.paths.to_vec(),
        globs: options.globs.to_vec(),
        max_count: options.max_count,
        matched_lines: result.matched_lines,
        truncated,
        scanned_chunks: result.scanned_chunks,
        matches,
        files,
    }
}

fn format_text_matches(matches: &[GrepMatch]) -> String {
    let matching_lines: BTreeSet<(String, usize)> =
        matches.iter().map(|m| (m.path.clone(), m.line)).collect();
    let mut emitted_context = BTreeSet::new();
    let mut current_path: Option<&str> = None;
    let mut lines = Vec::new();

    for item in matches {
        for context in &item.before {
            let key = (item.path.clone(), context.line);
            if !matching_lines.contains(&key) && emitted_context.insert(key) {
                push_grouped_grep_line(
                    &mut lines,
                    &mut current_path,
                    &item.path,
                    context.line,
                    '-',
                    &context.text,
                );
            }
        }

        push_grouped_grep_line(
            &mut lines,
            &mut current_path,
            &item.path,
            item.line,
            ':',
            &item.text,
        );

        for context in &item.after {
            let key = (item.path.clone(), context.line);
            if !matching_lines.contains(&key) && emitted_context.insert(key) {
                push_grouped_grep_line(
                    &mut lines,
                    &mut current_path,
                    &item.path,
                    context.line,
                    '-',
                    &context.text,
                );
            }
        }
    }

    lines.join("\n")
}

fn push_grouped_grep_line<'a>(
    lines: &mut Vec<String>,
    current_path: &mut Option<&'a str>,
    path: &'a str,
    line: usize,
    marker: char,
    text: &str,
) {
    if *current_path != Some(path) {
        lines.push(path.to_string());
        *current_path = Some(path);
    }
    lines.push(format!("{line}{marker}{}", text.trim_start()));
}

#[cfg(test)]
#[path = "grep/tests.rs"]
mod tests;
