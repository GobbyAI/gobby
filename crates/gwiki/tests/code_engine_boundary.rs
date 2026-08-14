use std::collections::VecDeque;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use gobby_core::ai::generation::{
    ChatCompletion, ChatCompletionRequest, ChatMessage, ChatTransport, StopReason, ToolCall,
    ToolError, ToolExecutor, ToolLoopLimits, ToolSchema, run_tool_loop,
};
use serde_json::json;

fn rust_sources(root: &Path) -> Vec<PathBuf> {
    let mut pending = vec![root.to_path_buf()];
    let mut sources = Vec::new();

    while let Some(directory) = pending.pop() {
        for entry in fs::read_dir(&directory)
            .unwrap_or_else(|error| panic!("failed to read {}: {error}", directory.display()))
        {
            let path = entry.expect("directory entry is readable").path();
            if path.is_dir() {
                pending.push(path);
            } else if path.extension().is_some_and(|extension| extension == "rs") {
                sources.push(path);
            }
        }
    }

    sources.sort();
    sources
}

#[test]
fn moved_engine_uses_only_facade() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("src/commands/code");
    let sources = rust_sources(&root);

    assert!(!sources.is_empty(), "moved CodeWiki engine must exist");
    for path in sources {
        let source = fs::read_to_string(&path)
            .unwrap_or_else(|error| panic!("failed to read {}: {error}", path.display()));
        for forbidden in [
            "gobby_code::db",
            "gobby_code::graph",
            "gobby_code::models",
            "gobby_code::visibility",
            "postgres::",
            "GWIKI_TEST_DATABASE_URL",
            "GCODE_TEST_DATABASE_URL",
            "GOBBY_TEST_POSTGRES_DSN",
            "bootstrap.yaml",
            "database_url_from_bootstrap",
            "postgres_database_url_from_bootstrap_file",
        ] {
            assert!(
                !source.contains(forbidden),
                "{} imports forbidden datastore surface {forbidden}",
                path.display()
            );
        }
        for import in source
            .lines()
            .filter(|line| line.contains("use gobby_code::index"))
        {
            assert_eq!(
                import.trim(),
                "use gobby_code::index::languages::is_supported_language;",
                "{} imports an unsupported gcode index surface",
                path.display()
            );
        }
    }
}

#[test]
fn relocation_inventory_and_composer_decomposition() {
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR"));
    let legacy_root = manifest.join("../gcode/src/commands/codewiki");
    let destination_root = manifest.join("src/commands/code");
    let destination_files = rust_sources(&destination_root);

    assert!(
        !legacy_root.exists(),
        "legacy gcode codewiki engine must stay deleted: {}",
        legacy_root.display()
    );
    assert_eq!(
        destination_files.len(),
        119,
        "moved inventory must match the plan"
    );
    for relative in [
        "diagram_compose/mod.rs",
        "diagram_compose/evidence.rs",
        "diagram_compose/candidates.rs",
        "diagram_compose/generation.rs",
        "command.rs",
        "runtime.rs",
    ] {
        assert!(
            destination_root.join(relative).is_file(),
            "missing {relative}"
        );
    }
    assert!(!destination_root.join("diagram_compose.rs").exists());
    for path in destination_files {
        let relative = path
            .strip_prefix(&destination_root)
            .expect("destination is below root");
        if relative.starts_with("tests") {
            continue;
        }
        let lines = fs::read_to_string(&path)
            .unwrap_or_else(|error| panic!("failed to read {}: {error}", path.display()))
            .lines()
            .count();
        assert!(lines < 1_000, "{} has {lines} lines", path.display());
    }
}

#[test]
fn runtime_carries_non_datastore_context() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("src/commands/code");
    let runtime = fs::read_to_string(root.join("runtime.rs")).expect("runtime source is readable");
    for field in [
        "project_root: PathBuf",
        "project_id: String",
        "quiet: bool",
        "verbose: bool",
        "ai: AiContext",
        "facts: CodewikiFacts",
    ] {
        assert!(runtime.contains(field), "runtime missing `{field}`");
    }
    for forbidden in ["database_url", "postgres::", "crate::config::Context"] {
        assert!(
            !runtime.contains(forbidden),
            "runtime contains `{forbidden}`"
        );
    }

    let one_shot = fs::read_to_string(root.join("text/generation/one_shot.rs"))
        .expect("one-shot source is readable");
    let tool_loop = fs::read_to_string(root.join("text/generation/tool_loop.rs"))
        .expect("tool-loop source is readable");
    for route in ["AiRouting::Off", "AiRouting::Daemon"] {
        assert!(one_shot.contains(route), "one-shot omits `{route}`");
        assert!(tool_loop.contains(route), "tool loop omits `{route}`");
    }
    for (path, entrypoint, retired_entrypoint) in [
        ("run.rs", "pub(crate) fn run_summary(", "pub fn run("),
        (
            "run.rs",
            "pub(crate) fn repair_summary(",
            "pub fn run_repair(",
        ),
        (
            "compare.rs",
            "pub(crate) fn compare_to(",
            "pub fn run_compare(",
        ),
        (
            "purge.rs",
            "pub(crate) fn purge_summary(",
            "pub fn run_purge(",
        ),
    ] {
        let source = fs::read_to_string(root.join(path))
            .unwrap_or_else(|error| panic!("failed to read {path}: {error}"));
        assert!(source.contains(entrypoint), "{path} omits `{entrypoint}`");
        assert!(
            !source.contains(retired_entrypoint),
            "{path} retains retired entrypoint `{retired_entrypoint}`"
        );
    }
}

#[test]
fn code_public_surface_exports_only_cli_options() {
    let lib = fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join("src/lib.rs"))
        .expect("library source is readable");
    assert!(!lib.contains("pub mod commands;"));

    let export = lib
        .split_once("pub use commands::code::{")
        .and_then(|(_, rest)| rest.split_once("};"))
        .map(|(exports, _)| {
            exports
                .split(',')
                .map(str::trim)
                .filter(|name| !name.is_empty())
                .collect::<Vec<_>>()
        })
        .expect("Code CLI export block");
    assert_eq!(
        export,
        [
            "AiDepth",
            "CodeCommandOptions",
            "DEFAULT_CODE_GRAPH_EDGE_LIMIT",
            "ProseDepth",
            "VerifyScope",
        ]
    );
}

#[test]
fn ownership_identities_moved() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("src/commands/code");
    let tool_loop = fs::read_to_string(root.join("text/generation/tool_loop.rs"))
        .expect("tool-loop source is readable");
    let lock = fs::read_to_string(root.join("lock.rs")).expect("lock source is readable");

    assert!(tool_loop.contains("const DAEMON_AGENTIC_CALLER: &str = \"gwiki.code\""));
    assert!(tool_loop.contains("cli: \"gcode\""));
    assert!(lock.contains("another `gwiki code` run is already writing"));
}

#[test]
fn tool_executor_uses_only_facade() {
    let executor = fs::read_to_string(
        Path::new(env!("CARGO_MANIFEST_DIR")).join("src/commands/code/tool_executor.rs"),
    )
    .expect("executor source is readable");
    for dependency in [
        "CodewikiFacts::search_with",
        "CodewikiFacts::symbols_for_file",
        "CodewikiFacts::symbol_by_id",
        "CodewikiFacts::grep_with",
        "CodewikiFacts::callers",
        "CodewikiFacts::usages",
        "CodewikiFacts::imports",
    ] {
        assert!(
            executor.contains(dependency),
            "dependency table missing `{dependency}`"
        );
    }
    for tool in [
        "search_code",
        "outline_file",
        "read_symbol",
        "grep_repo",
        "read_file",
        "find_callers",
        "find_usages",
        "imports",
    ] {
        assert!(executor.contains(tool), "dependency table missing `{tool}`");
    }
    for forbidden in [
        "crate::config::Context",
        "postgres::",
        "fn connection(",
        "connect_readonly",
        "Mutex",
    ] {
        assert!(
            !executor.contains(forbidden),
            "executor contains `{forbidden}`"
        );
    }
}

#[test]
fn graph_outcomes_match_legacy_mapping() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("src/commands/code");
    let executor =
        fs::read_to_string(root.join("tool_executor.rs")).expect("executor source is readable");
    let graph = fs::read_to_string(root.join("graph.rs")).expect("graph source is readable");
    let frontmatter = fs::read_to_string(root.join("text/frontmatter.rs"))
        .expect("frontmatter source is readable");
    let diagrams = fs::read_to_string(root.join("render/diagrams.rs"))
        .expect("diagram renderer source is readable");

    for outcome in [
        "GraphOutcome::Available",
        "GraphOutcome::Truncated",
        "GraphOutcome::Empty",
        "GraphOutcome::Unavailable",
    ] {
        assert!(executor.contains(outcome), "executor omits `{outcome}`");
    }
    assert!(graph.contains("CodewikiGraph::truncated"));
    assert!(graph.contains("CodewikiGraph::unavailable"));
    assert!(!executor.contains("GRAPH_TRUNCATED"));
    assert!(executor.contains("Err(error) => Err(tool_err"));
    assert!(graph.contains("GraphOutcome::Unavailable { .. } => None"));
    assert!(frontmatter.contains("code != GRAPH_UNAVAILABLE"));
    assert!(diagrams.contains("source graph was truncated"));
}

struct ScriptedTransport {
    completions: Mutex<VecDeque<ChatCompletion>>,
}

impl ChatTransport for ScriptedTransport {
    fn complete(
        &self,
        _request: ChatCompletionRequest<'_>,
    ) -> Result<ChatCompletion, gobby_core::ai_types::AiError> {
        Ok(self
            .completions
            .lock()
            .expect("completion lock")
            .pop_front()
            .expect("scripted completion"))
    }

    fn route(&self) -> &'static str {
        "test"
    }
}

fn tool_call_completion(name: &str, id: &str) -> ChatCompletion {
    ChatCompletion {
        content: None,
        tool_calls: vec![ToolCall {
            id: id.to_string(),
            name: name.to_string(),
            arguments: json!({}),
        }],
        finish_reason: Some("tool_calls".to_string()),
        model: Some("test".to_string()),
        usage: None,
    }
}

struct IsolatedExecutor {
    slow_completed: Arc<AtomicBool>,
    fast_completed: Arc<AtomicBool>,
    slow_release: Mutex<Receiver<()>>,
    slow_drained: Sender<()>,
}

impl ToolExecutor for IsolatedExecutor {
    fn schemas(&self) -> Vec<ToolSchema> {
        ["slow", "fast"]
            .into_iter()
            .map(|name| ToolSchema {
                name: name.to_string(),
                description: name.to_string(),
                parameters: json!({"type": "object"}),
            })
            .collect()
    }

    fn execute(&self, call: &ToolCall) -> Result<String, ToolError> {
        match call.name.as_str() {
            "slow" => {
                self.slow_release
                    .lock()
                    .expect("slow release receiver lock")
                    .recv()
                    .expect("slow call release signal");
                self.slow_completed.store(true, Ordering::SeqCst);
                self.slow_drained
                    .send(())
                    .expect("slow call completion signal");
                Ok("slow".to_string())
            }
            "fast" => {
                self.fast_completed.store(true, Ordering::SeqCst);
                Ok("fast".to_string())
            }
            name => Err(ToolError::new(format!("unknown tool {name}"))),
        }
    }
}

#[test]
fn tool_timeout_does_not_block_subsequent_calls() {
    let transport = ScriptedTransport {
        completions: Mutex::new(
            vec![
                tool_call_completion("slow", "slow-call"),
                tool_call_completion("fast", "fast-call"),
                ChatCompletion {
                    content: Some("recovered".to_string()),
                    tool_calls: Vec::new(),
                    finish_reason: Some("stop".to_string()),
                    model: Some("test".to_string()),
                    usage: None,
                },
            ]
            .into(),
        ),
    };
    let slow_completed = Arc::new(AtomicBool::new(false));
    let fast_completed = Arc::new(AtomicBool::new(false));
    let (slow_release, slow_release_rx) = mpsc::channel();
    let (slow_drained_tx, slow_drained) = mpsc::channel();
    let executor = Arc::new(IsolatedExecutor {
        slow_completed: Arc::clone(&slow_completed),
        fast_completed: Arc::clone(&fast_completed),
        slow_release: Mutex::new(slow_release_rx),
        slow_drained: slow_drained_tx,
    });
    let limits = ToolLoopLimits {
        max_turns: Some(4),
        tool_timeout_seconds: 1,
        loop_timeout_seconds: 8,
        ..ToolLoopLimits::default()
    };
    let outcome = run_tool_loop(
        &transport,
        executor,
        vec![ChatMessage::user("investigate")],
        &limits,
        None,
    )
    .expect("tool loop runs");

    assert_eq!(outcome.stop_reason, StopReason::Completed);
    assert_eq!(outcome.content.as_deref(), Some("recovered"));
    assert_eq!(outcome.observability.tool_call_count, 2);
    assert!(fast_completed.load(Ordering::SeqCst));
    assert!(!slow_completed.load(Ordering::SeqCst));
    slow_release.send(()).expect("release detached slow call");
    slow_drained
        .recv_timeout(Duration::from_secs(1))
        .expect("detached slow call drains after release");
    assert!(slow_completed.load(Ordering::SeqCst));
}
