use std::collections::{BTreeMap, BTreeSet};

use anyhow::Context as _;
use postgres::Client;
use postgres::types::ToSql;
use regex::Regex;
use serde::Serialize;

use crate::commands::scope;
use crate::config::{Context, ProjectIndexScope};
use crate::db;
use crate::output::{self, Format};
use crate::search::fts;
use crate::visibility;

pub struct GrepOptions<'a> {
    pub pattern: &'a str,
    pub paths: &'a [String],
    pub globs: &'a [String],
    pub fixed_strings: bool,
    pub ignore_case: bool,
    pub context: Option<usize>,
    pub before_context: Option<usize>,
    pub after_context: Option<usize>,
    pub max_count: Option<usize>,
    pub format: Format,
}

#[derive(Debug, Clone)]
struct IndexedContentChunk {
    file_path: String,
    line_start: usize,
    content: String,
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
    paths: Vec<String>,
    globs: Vec<String>,
    max_count: Option<usize>,
    matched_lines: usize,
    truncated: bool,
    scanned_chunks: usize,
    matches: Vec<GrepMatch>,
}

#[derive(Debug)]
struct GrepResult {
    scanned_chunks: usize,
    matched_lines: usize,
    truncated: bool,
    matches: Vec<GrepMatch>,
}

pub fn run(ctx: &Context, options: GrepOptions<'_>) -> anyhow::Result<()> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let filters = GrepFilters::new(options.paths, options.globs)?;
    let chunks = load_indexed_chunks(&mut conn, ctx, &filters)?;
    let result = grep_chunks_with_filters(&chunks, &options, &filters)?;

    match options.format {
        Format::Json => output::print_json(&GrepResponse {
            project_id: ctx.project_id.clone(),
            pattern: options.pattern.to_string(),
            fixed_strings: options.fixed_strings,
            ignore_case: options.ignore_case,
            paths: options.paths.to_vec(),
            globs: options.globs.to_vec(),
            max_count: options.max_count,
            matched_lines: result.matched_lines,
            truncated: result.truncated,
            scanned_chunks: result.scanned_chunks,
            matches: result.matches,
        }),
        Format::Text => {
            let text = format_text_matches(&result.matches);
            if text.is_empty() {
                Ok(())
            } else {
                output::print_text(&text)
            }
        }
    }
}

fn load_indexed_chunks(
    conn: &mut Client,
    ctx: &Context,
    filters: &GrepFilters,
) -> anyhow::Result<Vec<IndexedContentChunk>> {
    let mut chunks = Vec::new();
    let tombstone_language = visibility::TOMBSTONE_LANGUAGE;
    let rows = match &ctx.index_scope {
        ProjectIndexScope::Single => {
            let mut params: Vec<&(dyn ToSql + Sync)> = vec![&ctx.project_id, &tombstone_language];
            let mut conditions = vec![
                "c.project_id = $1".to_string(),
                "cf.language != $2".to_string(),
            ];
            push_grep_sql_prefilters(&mut conditions, &mut params, "c", filters);
            let sql = format!(
                "SELECT c.file_path,
                        c.line_start::BIGINT AS line_start,
                        c.content
                 FROM code_content_chunks c
                 JOIN code_indexed_files cf
                   ON cf.project_id = c.project_id AND cf.file_path = c.file_path
                 WHERE {}
                 ORDER BY c.file_path ASC, c.line_start ASC, c.chunk_index ASC",
                conditions.join(" AND ")
            );
            conn.query(&sql, &params)?
        }
        ProjectIndexScope::Overlay {
            overlay_project_id,
            parent_project_id,
            ..
        } => {
            let mut params: Vec<&(dyn ToSql + Sync)> =
                vec![overlay_project_id, parent_project_id, &tombstone_language];
            let mut conditions = vec![
                "cf.language != $3".to_string(),
                "(
                    c.project_id = $1
                    OR (
                        c.project_id = $2
                        AND NOT EXISTS (
                            SELECT 1 FROM code_indexed_files shadow
                            WHERE shadow.project_id = $1
                              AND shadow.file_path = c.file_path
                        )
                    )
                )"
                .to_string(),
            ];
            push_grep_sql_prefilters(&mut conditions, &mut params, "c", filters);
            let sql = format!(
                "SELECT c.file_path,
                        c.line_start::BIGINT AS line_start,
                        c.content
                 FROM code_content_chunks c
                 JOIN code_indexed_files cf
                   ON cf.project_id = c.project_id AND cf.file_path = c.file_path
                 WHERE {}
                 ORDER BY c.file_path ASC, c.line_start ASC, c.chunk_index ASC",
                conditions.join(" AND ")
            );
            conn.query(&sql, &params)?
        }
    };
    let mut valid_paths = BTreeMap::<String, bool>::new();
    for row in rows {
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
    Ok(chunks)
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
    let matcher = GrepMatcher::new(options.pattern, options.fixed_strings, options.ignore_case)?;
    let before_context = options.before_context.or(options.context).unwrap_or(0);
    let after_context = options.after_context.or(options.context).unwrap_or(0);

    let mut scanned_chunks = 0usize;
    let mut file_lines: BTreeMap<String, BTreeMap<usize, String>> = BTreeMap::new();
    let mut matches: BTreeMap<(String, usize), GrepMatch> = BTreeMap::new();

    for chunk in chunks {
        if !filters.matches(&chunk.file_path) {
            continue;
        }
        scanned_chunks += 1;

        for (offset, line_text) in chunk.content.lines().enumerate() {
            let line = chunk.line_start + offset;
            file_lines
                .entry(chunk.file_path.clone())
                .or_default()
                .entry(line)
                .or_insert_with(|| line_text.to_string());

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
    let max = options.max_count.unwrap_or(usize::MAX);
    let mut retained = matches.into_values().take(max).collect::<Vec<_>>();
    for item in &mut retained {
        if let Some(lines) = file_lines.get(&item.path) {
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

struct GrepMatcher {
    regex: Regex,
}

impl GrepMatcher {
    fn new(pattern: &str, fixed_strings: bool, ignore_case: bool) -> anyhow::Result<Self> {
        if pattern.is_empty() {
            anyhow::bail!("gcode grep pattern must not be empty");
        }
        let pattern = if fixed_strings {
            regex::escape(pattern)
        } else {
            pattern.to_string()
        };
        let regex = regex::RegexBuilder::new(&pattern)
            .case_insensitive(ignore_case)
            .build()
            .with_context(|| "invalid gcode grep pattern")?;
        Ok(Self { regex })
    }

    fn find_spans(&self, line: &str) -> Vec<GrepSpan> {
        self.regex
            .find_iter(line)
            .filter(|m| m.start() != m.end())
            .map(|m| GrepSpan {
                start: m.start(),
                end: m.end(),
            })
            .collect()
    }
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
        return Some(Vec::new());
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
    Some(prefixes)
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

fn i64_to_usize(value: i64, column: &str) -> anyhow::Result<usize> {
    value
        .try_into()
        .with_context(|| format!("column `{column}` contains negative or too-large value {value}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn chunk(path: &str, line_start: usize, content: &str) -> IndexedContentChunk {
        IndexedContentChunk {
            file_path: path.to_string(),
            line_start,
            content: content.to_string(),
        }
    }

    fn options(pattern: &str) -> GrepOptions<'_> {
        GrepOptions {
            pattern,
            paths: &[],
            globs: &[],
            fixed_strings: false,
            ignore_case: false,
            context: None,
            before_context: None,
            after_context: None,
            max_count: None,
            format: Format::Json,
        }
    }

    #[test]
    fn text_renders_grouped_grep_shape() {
        let chunks = vec![chunk("src/lib.rs", 1, "one\nneedle\nthree")];
        let result = grep_chunks(&chunks, &options("needle")).expect("grep chunks");

        assert_eq!(format_text_matches(&result.matches), "src/lib.rs\n2:needle");
    }

    #[test]
    fn text_groups_multiple_files() {
        let chunks = vec![
            chunk("src/a.rs", 1, "needle a"),
            chunk("tests/b.rs", 10, "needle b"),
        ];
        let result = grep_chunks(&chunks, &options("needle")).expect("grep chunks");

        assert_eq!(
            format_text_matches(&result.matches),
            "src/a.rs\n1:needle a\ntests/b.rs\n10:needle b"
        );
    }

    #[test]
    fn ordering_is_path_then_line() {
        let chunks = vec![
            chunk("b.rs", 10, "needle later"),
            chunk("a.rs", 3, "needle first"),
            chunk("a.rs", 1, "needle earliest"),
        ];
        let result = grep_chunks(&chunks, &options("needle")).expect("grep chunks");

        let keys: Vec<_> = result
            .matches
            .iter()
            .map(|m| (m.path.as_str(), m.line))
            .collect();
        assert_eq!(keys, vec![("a.rs", 1), ("a.rs", 3), ("b.rs", 10)]);
    }

    #[test]
    fn ignore_case_matches_case_insensitively() {
        let chunks = vec![chunk("src/lib.rs", 1, "Needle")];
        let mut opts = options("needle");
        opts.ignore_case = true;
        let result = grep_chunks(&chunks, &opts).expect("grep chunks");

        assert_eq!(result.matches.len(), 1);
    }

    #[test]
    fn fixed_strings_treat_regex_metacharacters_literally() {
        let chunks = vec![chunk("src/lib.rs", 1, "a.b\naxb")];
        let mut opts = options("a.b");
        opts.fixed_strings = true;
        let result = grep_chunks(&chunks, &opts).expect("grep chunks");

        assert_eq!(result.matches.len(), 1);
        assert_eq!(result.matches[0].line, 1);
    }

    #[test]
    fn sql_prefix_prefilter_requires_convertible_globs() {
        let paths = vec!["src/foo_bar".to_string(), "src/foo_bar/**".to_string()];
        assert_eq!(
            sql_like_prefixes(&paths).expect("path prefixes"),
            vec!["src/foo\\_bar%", "src/foo\\_bar/%"]
        );

        let globs = vec!["*.rs".to_string(), "src/*.rs".to_string()];
        assert_eq!(
            sql_like_prefixes(&globs).expect("glob prefixes"),
            vec!["src/%"]
        );
    }

    #[test]
    fn context_flags_include_bounded_neighbors() {
        let chunks = vec![chunk("src/lib.rs", 1, "one\ntwo\nneedle\nfour\nfive")];
        let mut opts = options("needle");
        opts.before_context = Some(1);
        opts.after_context = Some(2);
        let result = grep_chunks(&chunks, &opts).expect("grep chunks");
        let item = &result.matches[0];

        assert_eq!(
            item.before,
            vec![GrepContextLine {
                line: 2,
                text: "two".to_string()
            }]
        );
        assert_eq!(
            item.after,
            vec![
                GrepContextLine {
                    line: 4,
                    text: "four".to_string()
                },
                GrepContextLine {
                    line: 5,
                    text: "five".to_string()
                }
            ]
        );
        assert_eq!(
            format_text_matches(&result.matches),
            "src/lib.rs\n2-two\n3:needle\n4-four\n5-five"
        );
    }

    #[test]
    fn text_output_trims_leading_whitespace_without_changing_matches() {
        let chunks = vec![chunk(
            "src/lib.rs",
            1,
            "    before\n        needle\n\t\tafter",
        )];
        let mut opts = options("needle");
        opts.context = Some(1);
        let result = grep_chunks(&chunks, &opts).expect("grep chunks");
        let item = &result.matches[0];

        assert_eq!(item.text, "        needle");
        assert_eq!(item.before[0].text, "    before");
        assert_eq!(item.after[0].text, "\t\tafter");
        assert_eq!(
            format_text_matches(&result.matches),
            "src/lib.rs\n1-before\n2:needle\n3-after"
        );
    }

    #[test]
    fn text_suppresses_duplicate_context_lines() {
        let chunks = vec![chunk(
            "src/lib.rs",
            1,
            "one\nneedle one\nmiddle\nneedle two\nfive",
        )];
        let mut opts = options("needle");
        opts.context = Some(1);
        let result = grep_chunks(&chunks, &opts).expect("grep chunks");

        assert_eq!(
            format_text_matches(&result.matches),
            "src/lib.rs\n1-one\n2:needle one\n3-middle\n4:needle two\n5-five"
        );
    }

    #[test]
    fn max_count_caps_retained_matches_not_total_matching_lines() {
        let chunks = vec![chunk(
            "src/lib.rs",
            1,
            "before\nneedle one\nmiddle\nneedle two\nafter",
        )];
        let mut opts = options("needle");
        opts.context = Some(1);
        opts.max_count = Some(1);
        let result = grep_chunks(&chunks, &opts).expect("grep chunks");

        assert_eq!(result.matched_lines, 2);
        assert!(result.truncated);
        assert_eq!(result.matches[0].line, 2);
        assert_eq!(result.matches[0].before.len(), 1);
        assert_eq!(result.matches[0].after.len(), 1);
        assert_eq!(
            format_text_matches(&result.matches),
            "src/lib.rs\n1-before\n2:needle one\n3-middle"
        );
    }

    #[test]
    fn json_match_contains_spans_and_context() {
        let chunks = vec![chunk("src/lib.rs", 1, "before\nneedle needle\nafter")];
        let mut opts = options("needle");
        opts.context = Some(1);
        let result = grep_chunks(&chunks, &opts).expect("grep chunks");
        let value = serde_json::to_value(&result.matches[0]).expect("serialize match");

        assert_eq!(value["path"], "src/lib.rs");
        assert_eq!(value["line"], 2);
        assert_eq!(value["text"], "needle needle");
        assert_eq!(value["spans"][0]["start"], 0);
        assert_eq!(value["spans"][0]["end"], 6);
        assert_eq!(value["spans"][1]["start"], 7);
        assert_eq!(value["before"][0]["line"], 1);
        assert_eq!(value["after"][0]["line"], 3);
    }

    #[test]
    fn path_and_glob_filters_compose() {
        let chunks = vec![
            chunk("src/gobby/app.py", 1, "needle"),
            chunk("src/gobby/app.rs", 1, "needle"),
            chunk("tests/app.py", 1, "needle"),
        ];
        let paths = vec!["src/gobby".to_string()];
        let globs = vec!["*.py".to_string()];
        let opts = GrepOptions {
            paths: &paths,
            globs: &globs,
            ..options("needle")
        };
        let result = grep_chunks(&chunks, &opts).expect("grep chunks");

        assert_eq!(result.scanned_chunks, 1);
        assert_eq!(result.matches[0].path, "src/gobby/app.py");
    }

    #[test]
    fn bare_globs_match_basenames_but_slash_globs_match_paths() {
        let chunks = vec![
            chunk("src/app.py", 1, "needle"),
            chunk("tests/app.py", 1, "needle"),
        ];
        let bare = vec!["*.py".to_string()];
        let slash = vec!["src/*.py".to_string()];

        let bare_result = grep_chunks(
            &chunks,
            &GrepOptions {
                globs: &bare,
                ..options("needle")
            },
        )
        .expect("bare glob grep");
        let slash_result = grep_chunks(
            &chunks,
            &GrepOptions {
                globs: &slash,
                ..options("needle")
            },
        )
        .expect("slash glob grep");

        assert_eq!(bare_result.matches.len(), 2);
        assert_eq!(slash_result.matches.len(), 1);
        assert_eq!(slash_result.matches[0].path, "src/app.py");
    }

    #[test]
    fn overlapping_chunks_dedupe_by_file_and_line() {
        let chunks = vec![
            chunk("src/lib.rs", 1, "needle\nother"),
            chunk("src/lib.rs", 1, "needle\nother"),
        ];
        let result = grep_chunks(&chunks, &options("needle")).expect("grep chunks");

        assert_eq!(result.matches.len(), 1);
    }
}
