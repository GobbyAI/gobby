#![allow(dead_code)] // local-loop executor unused after daemon-only routing

//! Read-only code-index `ToolExecutor` for the gwiki CodeWiki tool loop (#978).
//!
//! Wraps the existing read-only gcode index query internals (search, outline,
//! symbol, grep, file read, code graph) behind the gcore [`ToolExecutor`] trait
//! so a tool-calling model can investigate the index in-process while building
//! an aggregate narrative page. The executor owns the wiki runtime and calls
//! [`CodewikiFacts`](gobby_code::codewiki_facts::CodewikiFacts), whose operations
//! acquire independent read-only connections. It adapts facade results into
//! byte-bounded textual tool results.
//!
//! Graph tools return an explicit `graph-unavailable` result (recorded as
//! data-source/evidence degradation) when FalkorDB is not reachable, rather
//! than an empty result indistinguishable from "no edges found". That
//! degradation is surfaced on the generated page; it is never an AI-generation
//! failure and never hard-fails the page (the loop still produces narrative
//! from the remaining evidence).

use std::fmt::Write as _;
use std::path::Component;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

use gobby_code::codewiki_facts::{
    FileId, GraphNodeFact, GraphOutcome, GrepQuery, ScopeSelector, SearchQuery,
};
use gobby_core::ai::generation::{ToolCall, ToolError, ToolExecutor, ToolSchema};
use serde_json::{Value, json};

use super::{CodeEngineRuntime, CodewikiGraphAvailability, GRAPH_UNAVAILABLE, Symbol};

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

/// Datastore or runtime dependency used by each model-facing tool.
pub(crate) const CODEWIKI_TOOL_DEPENDENCIES: &[(&str, &str)] = &[
    ("search_code", "CodewikiFacts::search_with"),
    ("outline_file", "CodewikiFacts::symbols_for_file"),
    (
        "read_symbol",
        "CodewikiFacts::symbol_by_id + CodeEngineRuntime::project_root",
    ),
    ("grep_repo", "CodewikiFacts::grep_with"),
    ("read_file", "CodeEngineRuntime::project_root"),
    ("find_callers", "CodewikiFacts::callers"),
    ("find_usages", "CodewikiFacts::usages"),
    ("imports", "CodewikiFacts::imports"),
];

/// In-process tool executor over the read-only facade for one tool-loop generation.
pub(crate) struct CodewikiToolExecutor {
    runtime: CodeEngineRuntime,
    graph_availability: CodewikiGraphAvailability,
    /// Data-source degradation codes accumulated during the loop (e.g.
    /// [`GRAPH_UNAVAILABLE`] when a graph tool ran against an unreachable
    /// FalkorDB). Evidence degradation surfaced on the generated page; never an
    /// AI-generation failure, so it never hard-fails the page.
    graph_unavailable: AtomicBool,
}

impl CodewikiToolExecutor {
    pub(crate) fn new(
        runtime: &CodeEngineRuntime,
        graph_availability: CodewikiGraphAvailability,
    ) -> anyhow::Result<Self> {
        debug_assert_eq!(
            CODEWIKI_TOOL_DEPENDENCIES.len(),
            codewiki_tool_schemas().len()
        );
        Ok(Self {
            runtime: runtime.clone(),
            graph_availability,
            graph_unavailable: AtomicBool::new(false),
        })
    }

    /// Snapshot data-source degradation accumulated by all workers.
    pub(crate) fn data_source_degraded(&self) -> Vec<String> {
        self.graph_unavailable
            .load(Ordering::Relaxed)
            .then(|| GRAPH_UNAVAILABLE.to_string())
            .into_iter()
            .collect()
    }

    fn search_code(&self, args: &Value) -> Result<String, ToolError> {
        let query = arg_str(args, "query")?;
        let limit = arg_usize(args, "limit", DEFAULT_SEARCH_LIMIT, MAX_SEARCH_LIMIT);
        let search = SearchQuery {
            text: query.clone(),
            limit,
            language: arg_str_opt(args, "language"),
            kind: arg_str_opt(args, "kind"),
            paths: arg_str_opt(args, "path").into_iter().collect(),
        };
        let results = self
            .runtime
            .facts
            .search_with(&search)
            .map_err(|error| tool_err(error.to_string()))?
            .into_iter()
            .map(|hit| Symbol::from_fact(hit.symbol, &self.runtime.project_id))
            .collect::<Vec<_>>();
        if results.is_empty() {
            return Ok(format!("No symbols matched `{query}`."));
        }
        Ok(format_symbol_list(
            &format!("{} symbol(s) matching `{query}`", results.len()),
            &results,
        ))
    }

    fn outline_file(&self, args: &Value) -> Result<String, ToolError> {
        let path = arg_str(args, "path")?;
        let symbols = self
            .runtime
            .facts
            .symbols_for_file(&FileId::new(path.clone()))
            .map_err(|error| tool_err(format!("outline_file failed for `{path}`: {error}")))?
            .into_iter()
            .map(|symbol| Symbol::from_fact(symbol, &self.runtime.project_id))
            .collect::<Vec<_>>();
        if symbols.is_empty() {
            return Ok(format!("`{path}` has no indexed symbols."));
        }
        Ok(format_symbol_list(
            &format!("{} symbol(s) in `{path}`", symbols.len()),
            &symbols,
        ))
    }

    fn read_symbol(&self, args: &Value) -> Result<String, ToolError> {
        let id = arg_str(args, "id")?;
        let symbol = self
            .runtime
            .facts
            .symbol_by_id(&id)
            .map_err(|error| tool_err(format!("read_symbol failed for `{id}`: {error}")))?
            .map(|symbol| Symbol::from_fact(symbol, &self.runtime.project_id));
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

    fn grep_repo(&self, args: &Value) -> Result<String, ToolError> {
        let pattern = arg_str(args, "pattern")?;
        let max_count = arg_usize(args, "max_count", DEFAULT_GREP_LIMIT, MAX_GREP_LIMIT);
        let scope = arg_str_opt(args, "path")
            .map(|path| ScopeSelector::paths([path]))
            .unwrap_or_else(ScopeSelector::all);
        let query = GrepQuery {
            pattern: pattern.clone(),
            scope,
            fixed_strings: args
                .get("fixed_strings")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            ignore_case: args
                .get("ignore_case")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            context: None,
            before_context: None,
            after_context: None,
            limit: max_count,
        };
        let result = self
            .runtime
            .facts
            .grep_with(&query)
            .map_err(|error| tool_err(format!("grep_repo failed for `{pattern}`: {error}")))?;
        if result.hits.is_empty() {
            return Ok(format!("No content matched `{pattern}`."));
        }
        let mut out = format!("{} match(es) for `{pattern}`:\n", result.hits.len());
        for m in &result.hits {
            let _ = writeln!(out, "{}:{}: {}", m.path, m.line, m.text.trim());
        }
        if result.truncated {
            out.push_str("(results truncated)\n");
        }
        Ok(out)
    }

    fn read_file(&self, args: &Value) -> Result<String, ToolError> {
        let path = arg_str(args, "path")?;
        let start_line = arg_usize(args, "start_line", 1, usize::MAX).max(1);
        let end_line = args
            .get("end_line")
            .and_then(Value::as_u64)
            .map(|value| value as usize);
        let full = safe_read_path(&self.runtime.project_root, &path)?;
        let content = std::fs::read_to_string(&full)
            .map_err(|error| tool_err(format!("cannot read `{path}`: {error}")))?;
        let lines: Vec<&str> = content.lines().collect();
        let total = lines.len();
        if start_line > total {
            return Ok(format!(
                "`{path}` has {total} line(s); start_line {start_line} is past the end."
            ));
        }
        let end = clamped_end_line(start_line, end_line, total);
        let mut body = String::new();
        for (offset, line) in lines[start_line - 1..end].iter().enumerate() {
            let _ = writeln!(body, "{}: {}", start_line + offset, line);
        }
        let fence = markdown_fence(&body);
        let mut out = format!("`{path}` lines {start_line}-{end} of {total}:\n{fence}\n{body}");
        out.push_str(&fence);
        Ok(out)
    }

    fn read_byte_snippet(&self, symbol: &Symbol) -> Result<String, ToolError> {
        read_byte_snippet_from_root(&self.runtime.project_root, symbol)
    }

    /// Run a code-graph query, mapping an unreachable/unconfigured FalkorDB to
    /// an explicit `graph-unavailable` result (recorded as evidence
    /// degradation) instead of an indistinguishable empty result. A genuine
    /// query error surfaces to the model as a tool error.
    fn graph_tool<F>(&self, tool: &str, query: F) -> Result<String, ToolError>
    where
        F: FnOnce() -> anyhow::Result<GraphOutcome<GraphNodeFact>>,
    {
        if self.graph_availability != CodewikiGraphAvailability::Available {
            self.graph_unavailable.store(true, Ordering::Relaxed);
            return Ok(format!(
                "{GRAPH_UNAVAILABLE}: the code graph (FalkorDB) is not available for this run; \
                 rely on search/outline/symbol/grep evidence instead."
            ));
        }
        match query() {
            Ok(GraphOutcome::Available(results)) => Ok(format_graph_results(tool, &results)),
            Ok(GraphOutcome::Truncated(results)) => {
                let mut output = format_graph_results(tool, &results);
                output.push_str("(results truncated)\n");
                Ok(output)
            }
            Ok(GraphOutcome::Empty) => Ok(format_graph_results(tool, &[])),
            Ok(GraphOutcome::Unavailable { reason }) => {
                self.graph_unavailable.store(true, Ordering::Relaxed);
                Ok(format!(
                    "{GRAPH_UNAVAILABLE}: the code graph (FalkorDB) is unreachable ({reason}); \
                     rely on other evidence instead."
                ))
            }
            Err(error) => Err(tool_err(format!("{tool} failed: {error}"))),
        }
    }
}

fn markdown_fence(content: &str) -> String {
    let longest = content
        .split(|character| character != '`')
        .map(str::len)
        .max()
        .unwrap_or_default();
    "`".repeat(3.max(longest + 1))
}

fn safe_read_path(project_root: &Path, relative_path: &str) -> Result<PathBuf, ToolError> {
    let relative = Path::new(relative_path);
    if relative.is_absolute()
        || relative.components().any(|component| {
            matches!(
                component,
                Component::ParentDir | Component::RootDir | Component::Prefix(_)
            )
        })
    {
        return Err(tool_err(format!(
            "path `{relative_path}` is outside the project root or unsafe to read"
        )));
    }
    let root = project_root
        .canonicalize()
        .map_err(|error| tool_err(format!("cannot resolve project root: {error}")))?;
    let full = root.join(relative);
    match full.canonicalize() {
        Ok(resolved) if resolved.starts_with(&root) => Ok(resolved),
        Ok(_) => Err(tool_err(format!(
            "path `{relative_path}` is outside the project root or unsafe to read"
        ))),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(full),
        Err(error) => Err(tool_err(format!(
            "cannot resolve `{relative_path}`: {error}"
        ))),
    }
}

fn clamped_end_line(start_line: usize, requested_end: Option<usize>, total: usize) -> usize {
    let default_end = start_line.saturating_add(DEFAULT_READ_LINES - 1);
    let max_end = start_line.saturating_add(MAX_READ_LINES - 1);
    requested_end
        .unwrap_or(default_end)
        .max(start_line)
        .min(max_end)
        .min(total)
}

fn read_byte_snippet_from_root(project_root: &Path, symbol: &Symbol) -> Result<String, ToolError> {
    let full = safe_read_path(project_root, &symbol.file_path)?;
    let bytes = std::fs::read(&full)
        .map_err(|error| tool_err(format!("cannot read symbol source: {error}")))?;
    let start = symbol.byte_start.min(bytes.len());
    let end = symbol
        .byte_end
        .min(bytes.len())
        .min(start.saturating_add(MAX_SNIPPET_BYTES));
    if end <= start {
        return Ok(String::new());
    }
    Ok(String::from_utf8_lossy(&bytes[start..end]).into_owned())
}

impl ToolExecutor for CodewikiToolExecutor {
    fn schemas(&self) -> Vec<ToolSchema> {
        codewiki_tool_schemas()
    }

    fn execute(&self, call: &ToolCall) -> Result<String, ToolError> {
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
                self.graph_tool("find_callers", || self.runtime.facts.callers(&id, limit))
            }
            "find_usages" => {
                let id = arg_str(args, "id")?;
                let limit = arg_usize(args, "limit", DEFAULT_GRAPH_LIMIT, MAX_GRAPH_LIMIT);
                self.graph_tool("find_usages", || self.runtime.facts.usages(&id, limit))
            }
            "imports" => {
                let path = arg_str(args, "path")?;
                let limit = arg_usize(args, "limit", DEFAULT_GRAPH_LIMIT, MAX_GRAPH_LIMIT);
                self.graph_tool("imports", || self.runtime.facts.imports(&path, limit))
            }
            other => Err(tool_err(format!("unknown tool `{other}`"))),
        }
    }
}

/// The investigation tool schemas advertised to the model for a tool-loop run.
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
                    "path": {"type": "string", "description": "Project-relative file path."},
                    "limit": {"type": "integer", "maximum": 50, "description": "Max imports (default 25, max 50)."}
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

fn format_graph_results(tool: &str, results: &[GraphNodeFact]) -> String {
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
        let imports = schemas
            .iter()
            .find(|schema| schema.name == "imports")
            .expect("imports schema");
        let limit = &imports.parameters["properties"]["limit"];
        assert_eq!(limit["type"], "integer");
        assert_eq!(limit["maximum"], 50);
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
    fn read_file_range_clamps_requested_end_to_start_and_caps() {
        assert_eq!(clamped_end_line(10, Some(5), 25), 10);
        assert_eq!(
            clamped_end_line(10, Some(10_000), 10_000),
            10 + MAX_READ_LINES - 1
        );
        assert_eq!(clamped_end_line(10, None, 12), 12);
    }

    #[test]
    fn read_file_range_saturates_end_calculations() {
        assert_eq!(
            clamped_end_line(usize::MAX - 1, None, usize::MAX),
            usize::MAX
        );
        assert_eq!(
            clamped_end_line(usize::MAX - 1, Some(usize::MAX), usize::MAX),
            usize::MAX
        );
    }

    #[test]
    fn markdown_fence_exceeds_embedded_backtick_runs() {
        assert_eq!(markdown_fence("plain"), "```");
        assert_eq!(markdown_fence("nested ``` fence"), "````");
        assert_eq!(markdown_fence("nested ````` fence"), "``````");
    }

    #[cfg(unix)]
    #[test]
    fn read_byte_snippet_rejects_symlink_outside_project() {
        let project = tempfile::tempdir().expect("project tempdir");
        let outside = tempfile::tempdir().expect("outside tempdir");
        let outside_file = outside.path().join("secret.rs");
        std::fs::write(&outside_file, "fn secret() {}\n").expect("outside file");
        std::os::unix::fs::symlink(&outside_file, project.path().join("link.rs")).expect("symlink");
        let symbol = Symbol {
            id: "sym-1".to_string(),
            project_id: "p".to_string(),
            file_path: "link.rs".to_string(),
            name: "secret".to_string(),
            qualified_name: "secret".to_string(),
            kind: "function".to_string(),
            language: "rust".to_string(),
            byte_start: 0,
            byte_end: 10,
            line_start: 1,
            line_end: 1,
            signature: None,
            docstring: None,
            parent_symbol_id: None,
            file_content_hash: String::new(),
            content_hash: String::new(),
            summary: None,
        };

        let error = read_byte_snippet_from_root(project.path(), &symbol)
            .expect_err("outside symlink rejected");

        assert!(error.message.contains("unsafe to read"), "{error:?}");
    }

    #[test]
    fn graph_result_formatting_lists_endpoints() {
        let results = vec![GraphNodeFact {
            id: "sym-1".to_string(),
            name: "caller_fn".to_string(),
            file_path: "src/lib.rs".to_string(),
            line: 12,
            confidence: "exact".to_string(),
            relation: Some("calls".to_string()),
            distance: None,
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
            file_content_hash: String::new(),
            content_hash: String::new(),
            summary: None,
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
