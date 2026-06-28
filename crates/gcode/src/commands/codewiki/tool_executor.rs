//! gcode `ToolExecutor` for CodeWiki Lane B (#978).
//!
//! Wraps the existing read-only gcode index query internals (search, outline,
//! symbol, grep, file read, code graph) behind the gcore [`ToolExecutor`] trait
//! so a tool-calling model can investigate the index in-process while building
//! an aggregate narrative page. The executor owns a read-only PostgreSQL
//! connection and the project [`Context`]; it builds no new query
//! infrastructure, only adapts the result-returning helpers beneath the CLI
//! print layer into byte-bounded textual tool results.
//!
//! Graph tools return an explicit `graph-unavailable` result (recorded as
//! data-source/evidence degradation) when FalkorDB is not reachable, rather
//! than an empty result indistinguishable from "no edges found". That
//! degradation is surfaced on the generated page; it is never an AI-generation
//! failure and never hard-fails the page (the loop still produces narrative
//! from the remaining evidence).

use std::collections::BTreeSet;
use std::fmt::Write as _;

use gobby_core::ai::generation::{ToolCall, ToolError, ToolExecutor, ToolSchema};
use serde_json::{Value, json};

use crate::commands::grep::{self, GrepOptions};
use crate::config::Context;
use crate::graph::code_graph;
use crate::index::security;
use crate::models::{GraphResult, Symbol};
use crate::output::Format;
use crate::search::fts;
use crate::{db, visibility};

use super::{CodewikiGraphAvailability, GRAPH_UNAVAILABLE};

/// Per-call result caps. The tool loop additionally truncates each result on a
/// UTF-8 boundary at `max_bytes_per_tool_result`; these bound the row counts
/// before that so the model sees whole records.
const DEFAULT_SEARCH_LIMIT: usize = 20;
const MAX_SEARCH_LIMIT: usize = 50;
const DEFAULT_GRAPH_LIMIT: usize = 25;
const MAX_GRAPH_LIMIT: usize = 50;
const DEFAULT_GREP_LIMIT: usize = 30;
const MAX_GREP_LIMIT: usize = 60;
const DEFAULT_READ_LINES: usize = 200;
const MAX_READ_LINES: usize = 400;
const MAX_SNIPPET_BYTES: usize = 4_000;

/// In-process tool executor over the gcode index for a single Lane B
/// generation. Holds a read-only connection and the project context; records
/// any data-source (graph) degradation hit during the loop.
pub(crate) struct CodewikiToolExecutor<'a> {
    ctx: &'a Context,
    conn: postgres::Client,
    graph_availability: CodewikiGraphAvailability,
    /// Data-source degradation codes accumulated during the loop (e.g.
    /// [`GRAPH_UNAVAILABLE`] when a graph tool ran against an unreachable
    /// FalkorDB). Evidence degradation surfaced on the generated page; never an
    /// AI-generation failure, so it never hard-fails the page.
    data_source_degraded: BTreeSet<String>,
}

impl<'a> CodewikiToolExecutor<'a> {
    /// Open a read-only connection for the executor. The caller threads the
    /// run's resolved [`CodewikiGraphAvailability`] so graph tools can return an
    /// explicit `graph-unavailable` result without re-probing per call.
    pub(crate) fn new(
        ctx: &'a Context,
        graph_availability: CodewikiGraphAvailability,
    ) -> anyhow::Result<Self> {
        let conn = db::connect_readonly(&ctx.database_url)?;
        Ok(Self {
            ctx,
            conn,
            graph_availability,
            data_source_degraded: BTreeSet::new(),
        })
    }

    /// The data-source degradation codes accumulated during the loop, drained
    /// for recording into the generated page's `degraded_sources`.
    pub(crate) fn into_data_source_degraded(self) -> Vec<String> {
        self.data_source_degraded.into_iter().collect()
    }

    fn search_code(&mut self, args: &Value) -> Result<String, ToolError> {
        let query = arg_str(args, "query")?;
        let limit = arg_usize(args, "limit", DEFAULT_SEARCH_LIMIT, MAX_SEARCH_LIMIT);
        let language = arg_str_opt(args, "language");
        let kind = arg_str_opt(args, "kind");
        let paths = arg_str_opt(args, "path")
            .map(|p| vec![p])
            .unwrap_or_default();
        let outcome = fts::search_symbols_exact_first_visible(
            &mut self.conn,
            &query,
            self.ctx,
            kind.as_deref(),
            language.as_deref(),
            &paths,
            limit,
        );
        if outcome.results.is_empty() {
            return Ok(format!("No symbols matched `{query}`."));
        }
        Ok(format_symbol_list(
            &format!("{} symbol(s) matching `{query}`", outcome.results.len()),
            &outcome.results,
        ))
    }

    fn outline_file(&mut self, args: &Value) -> Result<String, ToolError> {
        let path = arg_str(args, "path")?;
        let symbols = visibility::visible_symbols_for_file(&mut self.conn, self.ctx, &path)
            .map_err(|error| tool_err(format!("outline_file failed for `{path}`: {error}")))?;
        if symbols.is_empty() {
            return Ok(format!("`{path}` has no indexed symbols."));
        }
        Ok(format_symbol_list(
            &format!("{} symbol(s) in `{path}`", symbols.len()),
            &symbols,
        ))
    }

    fn read_symbol(&mut self, args: &Value) -> Result<String, ToolError> {
        let id = arg_str(args, "id")?;
        let symbol = visibility::visible_symbol_by_id(&mut self.conn, self.ctx, &id)
            .map_err(|error| tool_err(format!("read_symbol failed for `{id}`: {error}")))?;
        let Some(symbol) = symbol else {
            return Ok(format!("No visible symbol with id `{id}`."));
        };
        let mut out = format_symbol_detail(&symbol);
        match self.read_byte_snippet(&symbol) {
            Ok(snippet) if !snippet.trim().is_empty() => {
                out.push_str("\n\nSource:\n```\n");
                out.push_str(&snippet);
                out.push_str("\n```");
            }
            _ => {}
        }
        Ok(out)
    }

    fn grep_repo(&mut self, args: &Value) -> Result<String, ToolError> {
        let pattern = arg_str(args, "pattern")?;
        let max_count = arg_usize(args, "max_count", DEFAULT_GREP_LIMIT, MAX_GREP_LIMIT);
        let paths = arg_str_opt(args, "path")
            .map(|p| vec![p])
            .unwrap_or_default();
        let options = GrepOptions {
            pattern: &pattern,
            paths: &paths,
            globs: &[],
            fixed_strings: args
                .get("fixed_strings")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            ignore_case: args
                .get("ignore_case")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            word: false,
            context: None,
            before_context: None,
            after_context: None,
            max_count: Some(max_count),
            format: Format::Text,
        };
        let result = grep::grep_repo(self.ctx, &mut self.conn, &options)
            .map_err(|error| tool_err(format!("grep_repo failed for `{pattern}`: {error}")))?;
        if result.matches.is_empty() {
            return Ok(format!("No content matched `{pattern}`."));
        }
        let mut out = format!("{} match(es) for `{pattern}`:\n", result.matches.len());
        for m in &result.matches {
            let _ = writeln!(out, "{}:{}: {}", m.path, m.line, m.text.trim());
        }
        if result.truncated {
            out.push_str("(results truncated)\n");
        }
        Ok(out)
    }

    fn read_file(&mut self, args: &Value) -> Result<String, ToolError> {
        let path = arg_str(args, "path")?;
        let start_line = arg_usize(args, "start_line", 1, usize::MAX).max(1);
        let end_line = args
            .get("end_line")
            .and_then(Value::as_u64)
            .map(|value| value as usize);
        let full = self.ctx.project_root.join(&path);
        if !security::is_symlink_safe(&full, &self.ctx.project_root)
            || !security::validate_path(&full, &self.ctx.project_root)
        {
            return Err(tool_err(format!(
                "path `{path}` is outside the project root or unsafe to read"
            )));
        }
        let content = std::fs::read_to_string(&full)
            .map_err(|error| tool_err(format!("cannot read `{path}`: {error}")))?;
        let lines: Vec<&str> = content.lines().collect();
        let total = lines.len();
        if start_line > total {
            return Ok(format!(
                "`{path}` has {total} line(s); start_line {start_line} is past the end."
            ));
        }
        let max_end = start_line + MAX_READ_LINES - 1;
        let end = end_line
            .unwrap_or(start_line + DEFAULT_READ_LINES - 1)
            .min(max_end)
            .min(total);
        let mut out = format!("`{path}` lines {start_line}-{end} of {total}:\n```\n");
        for (offset, line) in lines[start_line - 1..end].iter().enumerate() {
            let _ = writeln!(out, "{}: {}", start_line + offset, line);
        }
        out.push_str("```");
        Ok(out)
    }

    fn read_byte_snippet(&self, symbol: &Symbol) -> Result<String, ToolError> {
        let full = self.ctx.project_root.join(&symbol.file_path);
        if !security::validate_path(&full, &self.ctx.project_root) {
            return Err(tool_err(
                "symbol source path is outside the project".to_string(),
            ));
        }
        let bytes = std::fs::read(&full)
            .map_err(|error| tool_err(format!("cannot read symbol source: {error}")))?;
        let start = symbol.byte_start.min(bytes.len());
        let end = symbol
            .byte_end
            .min(bytes.len())
            .min(start + MAX_SNIPPET_BYTES);
        if end <= start {
            return Ok(String::new());
        }
        Ok(String::from_utf8_lossy(&bytes[start..end]).into_owned())
    }

    /// Run a code-graph query, mapping an unreachable/unconfigured FalkorDB to
    /// an explicit `graph-unavailable` result (recorded as evidence
    /// degradation) instead of an indistinguishable empty result. A genuine
    /// query error surfaces to the model as a tool error.
    fn graph_tool<F>(&mut self, tool: &str, query: F) -> Result<String, ToolError>
    where
        F: FnOnce() -> anyhow::Result<Vec<GraphResult>>,
    {
        if self.graph_availability != CodewikiGraphAvailability::Available {
            self.data_source_degraded
                .insert(GRAPH_UNAVAILABLE.to_string());
            return Ok(format!(
                "{GRAPH_UNAVAILABLE}: the code graph (FalkorDB) is not available for this run; \
                 rely on search/outline/symbol/grep evidence instead."
            ));
        }
        match query() {
            Ok(results) => Ok(format_graph_results(tool, &results)),
            Err(error) if is_graph_unavailable(&error) => {
                self.data_source_degraded
                    .insert(GRAPH_UNAVAILABLE.to_string());
                Ok(format!(
                    "{GRAPH_UNAVAILABLE}: the code graph (FalkorDB) is unreachable ({error}); \
                     rely on other evidence instead."
                ))
            }
            Err(error) => Err(tool_err(format!("{tool} failed: {error}"))),
        }
    }
}

impl ToolExecutor for CodewikiToolExecutor<'_> {
    fn schemas(&self) -> Vec<ToolSchema> {
        codewiki_tool_schemas()
    }

    fn execute(&mut self, call: &ToolCall) -> Result<String, ToolError> {
        let args = &call.arguments;
        match call.name.as_str() {
            "search_code" => self.search_code(args),
            "outline_file" => self.outline_file(args),
            "read_symbol" => self.read_symbol(args),
            "grep_repo" => self.grep_repo(args),
            "read_file" => self.read_file(args),
            "find_callers" => {
                let id = arg_str(args, "id")?;
                let limit = arg_usize(args, "limit", DEFAULT_GRAPH_LIMIT, MAX_GRAPH_LIMIT);
                // Copy the `&Context` out of `self` so the query closure does not
                // borrow `self` while `graph_tool` takes `&mut self`.
                let ctx = self.ctx;
                self.graph_tool("find_callers", move || {
                    code_graph::find_callers(ctx, &id, 0, limit)
                })
            }
            "find_usages" => {
                let id = arg_str(args, "id")?;
                let limit = arg_usize(args, "limit", DEFAULT_GRAPH_LIMIT, MAX_GRAPH_LIMIT);
                let ctx = self.ctx;
                self.graph_tool("find_usages", move || {
                    code_graph::find_usages(ctx, &id, 0, limit)
                })
            }
            "imports" => {
                let path = arg_str(args, "path")?;
                let ctx = self.ctx;
                self.graph_tool("imports", move || code_graph::get_imports(ctx, &path))
            }
            other => Err(tool_err(format!("unknown tool `{other}`"))),
        }
    }
}

/// The investigation tool schemas advertised to the model for a Lane B run.
/// Free function so the schema surface can be asserted without a live index.
pub(crate) fn codewiki_tool_schemas() -> Vec<ToolSchema> {
    vec![
        tool_schema(
            "search_code",
            "Search the code index for symbols (functions, types, methods) by name or \
                 concept. Returns matching symbols with their id, kind, file, line range, and \
                 signature. Use the returned id with read_symbol/find_callers/find_usages.",
            json!({
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query (symbol name or concept)."},
                    "limit": {"type": "integer", "description": "Max results (default 20, max 50)."},
                    "language": {"type": "string", "description": "Optional language filter (e.g. rust)."},
                    "kind": {"type": "string", "description": "Optional symbol kind filter (e.g. function, struct)."},
                    "path": {"type": "string", "description": "Optional path prefix to scope the search."}
                },
                "required": ["query"]
            }),
        ),
        tool_schema(
            "outline_file",
            "List the indexed symbols defined in a file (its outline), with ids, kinds, and \
                 line ranges. Use to understand a file's structure before reading symbols.",
            json!({
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Project-relative file path."}
                },
                "required": ["path"]
            }),
        ),
        tool_schema(
            "read_symbol",
            "Read a single symbol by id (from search_code/outline_file): its signature, \
                 docstring, location, and source snippet.",
            json!({
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Symbol id from search_code or outline_file."}
                },
                "required": ["id"]
            }),
        ),
        tool_schema(
            "grep_repo",
            "Search indexed file content for a regex (or fixed string) pattern. Returns \
                 matching lines with file and line number. Use for text/config/comment evidence \
                 not captured as symbols.",
            json!({
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex (or fixed string) to search for."},
                    "fixed_strings": {"type": "boolean", "description": "Treat the pattern literally (default false)."},
                    "ignore_case": {"type": "boolean", "description": "Case-insensitive match (default false)."},
                    "max_count": {"type": "integer", "description": "Max matches (default 30, max 60)."},
                    "path": {"type": "string", "description": "Optional path prefix to scope the search."}
                },
                "required": ["pattern"]
            }),
        ),
        tool_schema(
            "read_file",
            "Read a bounded line range of a project file (default 200 lines, max 400). Use \
                 for content that is not symbol-shaped (configs, docs, manifests).",
            json!({
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Project-relative file path."},
                    "start_line": {"type": "integer", "description": "1-based first line (default 1)."},
                    "end_line": {"type": "integer", "description": "1-based last line (bounded to 400 lines)."}
                },
                "required": ["path"]
            }),
        ),
        tool_schema(
            "find_callers",
            "Find the callers of a symbol (by id) via the code graph. Returns caller symbols \
                 with file and line. Returns a graph-unavailable notice if the code graph backend \
                 is down.",
            json!({
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Symbol id from search_code or outline_file."},
                    "limit": {"type": "integer", "description": "Max callers (default 25, max 50)."}
                },
                "required": ["id"]
            }),
        ),
        tool_schema(
            "find_usages",
            "Find usages/references of a symbol (by id) via the code graph. Returns a \
                 graph-unavailable notice if the code graph backend is down.",
            json!({
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Symbol id from search_code or outline_file."},
                    "limit": {"type": "integer", "description": "Max usages (default 25, max 50)."}
                },
                "required": ["id"]
            }),
        ),
        tool_schema(
            "imports",
            "List the import targets of a file via the code graph. Returns a graph-unavailable \
                 notice if the code graph backend is down.",
            json!({
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Project-relative file path."}
                },
                "required": ["path"]
            }),
        ),
    ]
}

fn tool_schema(name: &str, description: &str, parameters: Value) -> ToolSchema {
    ToolSchema {
        name: name.to_string(),
        description: description.to_string(),
        parameters,
    }
}

fn tool_err(message: String) -> ToolError {
    ToolError { message }
}

fn arg_str(args: &Value, key: &str) -> Result<String, ToolError> {
    args.get(key)
        .and_then(Value::as_str)
        .map(str::to_string)
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| tool_err(format!("missing required string argument `{key}`")))
}

fn arg_str_opt(args: &Value, key: &str) -> Option<String> {
    args.get(key)
        .and_then(Value::as_str)
        .map(str::to_string)
        .filter(|value| !value.trim().is_empty())
}

fn arg_usize(args: &Value, key: &str, default: usize, max: usize) -> usize {
    args.get(key)
        .and_then(Value::as_u64)
        .map(|value| value as usize)
        .filter(|value| *value > 0)
        .unwrap_or(default)
        .min(max)
}

/// True when a code-graph error denotes an unavailable backend (unconfigured or
/// unreachable) rather than a genuine query failure.
fn is_graph_unavailable(error: &anyhow::Error) -> bool {
    matches!(
        error.downcast_ref::<code_graph::GraphReadError>(),
        Some(
            code_graph::GraphReadError::NotConfigured
                | code_graph::GraphReadError::Unreachable { .. }
        )
    )
}

fn format_symbol_list(header: &str, symbols: &[Symbol]) -> String {
    let mut out = format!("{header}:\n");
    for symbol in symbols {
        let signature = symbol.signature.as_deref().unwrap_or("");
        let _ = writeln!(
            out,
            "- {} [{}] {}:{}-{} id={}{}",
            symbol.qualified_name,
            symbol.kind,
            symbol.file_path,
            symbol.line_start,
            symbol.line_end,
            symbol.id,
            if signature.is_empty() {
                String::new()
            } else {
                format!("\n    {signature}")
            }
        );
    }
    out
}

fn format_symbol_detail(symbol: &Symbol) -> String {
    let mut out = format!(
        "{} [{}] in {}:{}-{}\nid: {}",
        symbol.qualified_name,
        symbol.kind,
        symbol.file_path,
        symbol.line_start,
        symbol.line_end,
        symbol.id,
    );
    if let Some(signature) = &symbol.signature {
        let _ = write!(out, "\nsignature: {signature}");
    }
    if let Some(docstring) = &symbol.docstring {
        let _ = write!(out, "\ndoc: {docstring}");
    }
    out
}

fn format_graph_results(tool: &str, results: &[GraphResult]) -> String {
    if results.is_empty() {
        return format!("{tool}: no results found.");
    }
    let mut out = format!("{tool}: {} result(s):\n", results.len());
    for result in results {
        let relation = result.relation.as_deref().unwrap_or("");
        let _ = writeln!(
            out,
            "- {} {}:{}{}{}",
            result.name,
            result.file_path,
            result.line,
            if result.id.is_empty() {
                String::new()
            } else {
                format!(" id={}", result.id)
            },
            if relation.is_empty() {
                String::new()
            } else {
                format!(" ({relation})")
            }
        );
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn schemas_advertise_the_eight_investigation_tools_with_valid_shapes() {
        let schemas = codewiki_tool_schemas();
        let names: Vec<&str> = schemas.iter().map(|schema| schema.name.as_str()).collect();
        assert_eq!(
            names,
            vec![
                "search_code",
                "outline_file",
                "read_symbol",
                "grep_repo",
                "read_file",
                "find_callers",
                "find_usages",
                "imports",
            ]
        );
        for schema in &schemas {
            assert!(
                !schema.description.trim().is_empty(),
                "{} has an empty description",
                schema.name
            );
            assert_eq!(
                schema.parameters.get("type").and_then(Value::as_str),
                Some("object"),
                "{} parameters must be a JSON Schema object",
                schema.name
            );
            assert!(
                schema.parameters.get("properties").is_some(),
                "{} must declare properties",
                schema.name
            );
        }
    }

    #[test]
    fn arg_parsing_applies_defaults_and_caps() {
        assert_eq!(arg_usize(&json!({"limit": 999}), "limit", 20, 50), 50);
        assert_eq!(arg_usize(&json!({}), "limit", 20, 50), 20);
        assert_eq!(arg_usize(&json!({"limit": 0}), "limit", 20, 50), 20);
        assert_eq!(arg_usize(&json!({"limit": 7}), "limit", 20, 50), 7);
        assert!(arg_str(&json!({"query": "  "}), "query").is_err());
        assert!(arg_str(&json!({}), "query").is_err());
        assert_eq!(
            arg_str(&json!({"query": "Symbol"}), "query").unwrap(),
            "Symbol"
        );
        assert_eq!(arg_str_opt(&json!({}), "path"), None);
        assert_eq!(
            arg_str_opt(&json!({"path": "src/lib.rs"}), "path").as_deref(),
            Some("src/lib.rs")
        );
    }

    #[test]
    fn graph_unavailable_error_is_distinguished_from_query_failure() {
        let unreachable = anyhow::Error::new(code_graph::GraphReadError::Unreachable {
            message: "connection refused".to_string(),
        });
        assert!(is_graph_unavailable(&unreachable));
        let not_configured = anyhow::Error::new(code_graph::GraphReadError::NotConfigured);
        assert!(is_graph_unavailable(&not_configured));
        let other = anyhow::anyhow!("some other failure");
        assert!(!is_graph_unavailable(&other));
    }

    #[test]
    fn graph_result_formatting_lists_endpoints() {
        let results = vec![GraphResult {
            id: "sym-1".to_string(),
            name: "caller_fn".to_string(),
            file_path: "src/lib.rs".to_string(),
            line: 12,
            confidence: Default::default(),
            relation: Some("calls".to_string()),
            distance: None,
            metadata: None,
        }];
        let rendered = format_graph_results("find_callers", &results);
        assert!(rendered.contains("caller_fn"));
        assert!(rendered.contains("src/lib.rs:12"));
        assert!(rendered.contains("calls"));
        assert!(format_graph_results("find_usages", &[]).contains("no results"));
    }

    #[test]
    fn symbol_formatting_includes_id_kind_and_location() {
        let symbol = Symbol {
            id: "sym-1".to_string(),
            project_id: "p".to_string(),
            file_path: "src/lib.rs".to_string(),
            name: "do_thing".to_string(),
            qualified_name: "mymod::do_thing".to_string(),
            kind: "function".to_string(),
            language: "rust".to_string(),
            byte_start: 0,
            byte_end: 10,
            line_start: 5,
            line_end: 9,
            signature: Some("fn do_thing()".to_string()),
            docstring: None,
            parent_symbol_id: None,
            content_hash: String::new(),
            summary: None,
            created_at: String::new(),
            updated_at: String::new(),
        };
        let list = format_symbol_list("1 symbol(s)", std::slice::from_ref(&symbol));
        assert!(list.contains("mymod::do_thing"));
        assert!(list.contains("[function]"));
        assert!(list.contains("src/lib.rs:5-9"));
        assert!(list.contains("id=sym-1"));
        let detail = format_symbol_detail(&symbol);
        assert!(detail.contains("signature: fn do_thing()"));
    }
}
