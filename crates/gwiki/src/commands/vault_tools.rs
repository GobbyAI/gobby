#![allow(dead_code)] // local-loop executor unused after daemon-only routing

//! Tool-loop vault executor for `gwiki compile`.
//!
//! Exposes the wiki vault's hybrid search and scoped document read as
//! model-callable tools — the gwiki analog of codewiki's `CodewikiToolExecutor`
//! (#978) — so the compile narrative model can investigate the indexed vault to
//! build its own grounding instead of writing from a single pre-assembled
//! prompt (#982).

use std::collections::BTreeSet;
use std::path::PathBuf;
use std::sync::Mutex;

use gobby_core::ai::generation::{ToolCall, ToolError, ToolExecutor, ToolSchema};
use serde_json::{Value, json};

use crate::ScopeSelection;
use crate::api::ScopeIdentity;
use crate::sources::SourceManifest;

use super::{backlinks, read, search};

const DEFAULT_SEARCH_LIMIT: usize = 8;
const MAX_SEARCH_LIMIT: usize = 25;
/// Per-result token budget for vault search, keeping each tool result well
/// inside the loop's per-result byte cap.
const SEARCH_TOKEN_BUDGET: usize = 1_500;

/// In-process executor over the indexed wiki vault, backing the compile tool loop
/// tool loop. Holds the resolved scope so every tool call runs against the same
/// vault as the compile request.
pub(crate) struct VaultToolExecutor {
    selection: ScopeSelection,
    vault_root: PathBuf,
    scope_identity: ScopeIdentity,
    /// Data-source degradation codes accumulated during the loop (e.g. a
    /// graph/semantic backend unavailable mid-search). Evidence degradation
    /// surfaced for observability; never an AI-generation failure, so it never
    /// hard-fails the page.
    data_source_degraded: Mutex<BTreeSet<String>>,
}

impl VaultToolExecutor {
    pub(crate) fn new(
        selection: ScopeSelection,
        vault_root: PathBuf,
        scope_identity: ScopeIdentity,
    ) -> Self {
        Self {
            selection,
            vault_root,
            scope_identity,
            data_source_degraded: Mutex::new(BTreeSet::new()),
        }
    }

    /// Data-source degradation codes accumulated during the loop, sorted.
    pub(crate) fn data_source_degraded(&self) -> Vec<String> {
        self.data_source_degraded
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .iter()
            .cloned()
            .collect()
    }

    fn search_vault(&self, args: &Value) -> Result<String, ToolError> {
        let query = arg_str(args, "query")?;
        let limit = arg_usize(args, "limit", DEFAULT_SEARCH_LIMIT, MAX_SEARCH_LIMIT);
        // Agent-facing vault search keeps the default surface: quarantined
        // candidate pages stay hidden until promoted (#17727).
        let output = search::retrieve(
            query.clone(),
            self.selection.clone(),
            limit,
            true,
            Some(SEARCH_TOKEN_BUDGET),
            false,
        )
        .map_err(|error| tool_err(format!("vault search failed: {error}")))?;
        let mut degraded = self
            .data_source_degraded
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        for degradation in &output.degradations {
            degraded.insert(degradation.clone());
        }
        drop(degraded);
        if output.results.is_empty() {
            return Ok(format!("No vault documents matched `{query}`."));
        }
        let mut block = format!(
            "{} vault document(s) matching `{query}`:\n",
            output.results.len()
        );
        for (index, result) in output.results.iter().enumerate() {
            let title = result
                .title
                .clone()
                .unwrap_or_else(|| result.wiki_page.display().to_string());
            block.push_str(&format!(
                "\n{}. {title}\n   path: {}\n   {}\n",
                index + 1,
                result.wiki_page.display(),
                result.snippet.trim()
            ));
        }
        Ok(block)
    }

    fn read_document(&self, args: &Value) -> Result<String, ToolError> {
        let path = arg_str(args, "path")?;
        read::read_document_text(
            &self.vault_root,
            self.scope_identity.clone(),
            PathBuf::from(&path),
        )
        .map_err(|error| tool_err(format!("read `{path}` failed: {error}")))
    }

    fn backlinks(&self, args: &Value) -> Result<String, ToolError> {
        let path = arg_str(args, "path")?;
        let limit = arg_usize(args, "limit", DEFAULT_SEARCH_LIMIT, MAX_SEARCH_LIMIT);
        let outcome = backlinks::execute(path.clone(), self.selection.clone())
            .map_err(|error| tool_err(format!("backlinks for `{path}` failed: {error}")))?;
        let mut records = outcome.result.payload["backlinks"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        records.truncate(limit);
        serde_json::to_string(&records)
            .map_err(|error| tool_err(format!("backlinks serialization failed: {error}")))
    }

    fn sources(&self, args: &Value) -> Result<String, ToolError> {
        let limit = arg_usize(args, "limit", DEFAULT_SEARCH_LIMIT, MAX_SEARCH_LIMIT);
        let manifest = SourceManifest::read(&self.vault_root)
            .map_err(|error| tool_err(format!("source manifest read failed: {error}")))?;
        let records = manifest.entries.into_iter().take(limit).collect::<Vec<_>>();
        serde_json::to_string(&records)
            .map_err(|error| tool_err(format!("source manifest serialization failed: {error}")))
    }
}

impl ToolExecutor for VaultToolExecutor {
    fn schemas(&self) -> Vec<ToolSchema> {
        vault_tool_schemas()
    }

    fn execute(&self, call: &ToolCall) -> Result<String, ToolError> {
        match call.name.as_str() {
            "search_vault" => self.search_vault(&call.arguments),
            "read_document" => self.read_document(&call.arguments),
            "backlinks" => self.backlinks(&call.arguments),
            "sources" => self.sources(&call.arguments),
            other => Err(tool_err(format!("unknown tool `{other}`"))),
        }
    }
}

/// Tool schemas advertised to the tool-loop model for vault investigation.
pub(crate) fn vault_tool_schemas() -> Vec<ToolSchema> {
    vec![
        tool_schema(
            "search_vault",
            "Hybrid (BM25 + semantic + graph) search over the indexed wiki vault. \
             Returns ranked document hits with title, wiki path, and a snippet. Use \
             this to discover which vault documents are relevant before reading them.",
            json!({
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language or keyword query."
                    },
                    "limit": {
                        "type": "integer",
                        "description": format!(
                            "Max results (default {DEFAULT_SEARCH_LIMIT}, max {MAX_SEARCH_LIMIT})."
                        ),
                        "minimum": 1,
                        "maximum": MAX_SEARCH_LIMIT
                    }
                },
                "required": ["query"]
            }),
        ),
        tool_schema(
            "read_document",
            "Read a single vault document by its wiki path (as returned by \
             search_vault). Returns the document title and bounded Markdown content.",
            json!({
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Vault-relative wiki path, e.g. `topics/example.md`."
                    }
                },
                "required": ["path"]
            }),
        ),
        tool_schema(
            "backlinks",
            "List vault pages linking to a wiki path. Results are bounded and read-only.",
            json!({
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Vault-relative target wiki path."
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_SEARCH_LIMIT
                    }
                },
                "required": ["path"]
            }),
        ),
        tool_schema(
            "sources",
            "List registered vault source records. Results are bounded and read-only.",
            json!({
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_SEARCH_LIMIT
                    }
                },
                "required": []
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
    ToolError::new(message)
}

fn arg_str(args: &Value, key: &str) -> Result<String, ToolError> {
    args.get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
        .ok_or_else(|| tool_err(format!("missing required string argument `{key}`")))
}

fn arg_usize(args: &Value, key: &str, default: usize, max: usize) -> usize {
    args.get(key)
        .and_then(Value::as_u64)
        .map(|value| (value as usize).clamp(1, max))
        .unwrap_or(default)
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;
    use std::collections::VecDeque;
    use std::fs;

    use crate::support::test_env::EnvGuard;
    use gobby_core::ai::generation::{
        ChatCompletion, ChatCompletionRequest, ChatMessage, ChatTransport, ToolCall,
        ToolLoopLimits, run_tool_loop,
    };
    use gobby_core::ai_types::AiError;

    use super::*;

    fn executor(root: &std::path::Path) -> VaultToolExecutor {
        VaultToolExecutor::new(
            ScopeSelection::Detect,
            root.to_path_buf(),
            ScopeIdentity::project("test-project"),
        )
    }

    #[test]
    fn schemas_advertise_exact_readonly_capabilities() {
        let schemas = vault_tool_schemas();
        let names: Vec<&str> = schemas.iter().map(|schema| schema.name.as_str()).collect();
        assert_eq!(
            names,
            vec!["search_vault", "read_document", "backlinks", "sources"]
        );
        for schema in &schemas {
            assert_eq!(schema.parameters["type"], "object");
            assert!(
                schema.parameters["required"].is_array(),
                "{} must declare required args",
                schema.name
            );
        }
    }

    #[test]
    fn sources_tool_returns_bounded_manifest_records() {
        use crate::sources::{SourceDraft, SourceKind, SourceManifest};

        let temp = tempfile::tempdir().expect("tempdir");
        SourceManifest::register(
            temp.path(),
            SourceDraft::new("first.md", SourceKind::Markdown, "2026-07-22", b"first"),
        )
        .expect("register first source");
        SourceManifest::register(
            temp.path(),
            SourceDraft::new("second.md", SourceKind::Markdown, "2026-07-22", b"second"),
        )
        .expect("register second source");

        let executor = executor(temp.path());
        let output = executor
            .execute(&ToolCall {
                id: "call-sources".to_string(),
                name: "sources".to_string(),
                arguments: json!({"limit": 1}),
            })
            .expect("sources tool executes");
        let records: Vec<Value> = serde_json::from_str(&output).expect("sources JSON");
        assert_eq!(records.len(), 1);
        assert!(records[0]["id"].is_string());
    }

    #[test]
    fn backlinks_tool_returns_bounded_vault_records() {
        let temp = tempfile::tempdir().expect("tempdir");
        let vault = temp.path().join("wiki");
        fs::create_dir_all(temp.path().join(".gobby")).expect("create project metadata");
        fs::write(
            temp.path().join(".gobby/project.json"),
            r#"{"id":"00000000-0000-4000-8000-000000000001"}"#,
        )
        .expect("write project identity");
        fs::create_dir_all(vault.join("_gwiki")).expect("create vault state");
        fs::write(
            vault.join("_gwiki/scope.json"),
            r#"{"kind":"project","id":"00000000-0000-4000-8000-000000000001"}"#,
        )
        .expect("write vault scope");
        fs::create_dir_all(vault.join("knowledge/concepts")).expect("create vault");
        fs::write(vault.join("knowledge/concepts/target.md"), "# Target\n").expect("write target");
        fs::write(
            vault.join("knowledge/concepts/source.md"),
            "# Source\n\nSee [[knowledge/concepts/target.md]].\n",
        )
        .expect("write source");
        let home = tempfile::tempdir().expect("home");
        let _env = EnvGuard::unset("GOBBY_MANAGED_EXECUTION_BOOTSTRAP")
            .and_set("GOBBY_HOME", home.path().as_os_str())
            .and_set("GOBBY_DAEMON_URL", "http://192.0.2.1:9");
        crate::support::env::set_active_project_root(Some(temp.path().to_path_buf()));
        let executor = VaultToolExecutor::new(
            ScopeSelection::project(temp.path()),
            vault,
            ScopeIdentity::project("00000000-0000-4000-8000-000000000001"),
        );

        let error = executor
            .execute(&ToolCall {
                id: "call-backlinks".to_string(),
                name: "backlinks".to_string(),
                arguments: json!({
                    "path": "knowledge/concepts/target.md",
                    "limit": 1,
                }),
            })
            .expect_err("backlinks require a grant-resolved graph");
        assert!(
            error.message.contains("daemon required")
                || error.message.contains("malformed grant")
                || error.message.contains("handshake"),
            "{error:?}"
        );
        crate::support::env::set_active_project_root(None);
    }

    fn write_vault_doc(root: &std::path::Path, body: &str) -> &'static str {
        let path = "knowledge/topics/overview.md";
        let absolute = root.join(path);
        fs::create_dir_all(absolute.parent().expect("parent")).expect("mkdir");
        fs::write(absolute, body).expect("write doc");
        path
    }

    #[test]
    fn read_document_returns_scoped_vault_content() {
        let temp = tempfile::tempdir().expect("tempdir");
        let path = write_vault_doc(temp.path(), "# Overview\n\nVault body text.");
        let executor = executor(temp.path());
        let call = ToolCall {
            id: "call-1".to_string(),
            name: "read_document".to_string(),
            arguments: json!({ "path": path }),
        };
        let result = executor.execute(&call).expect("read succeeds");
        assert!(
            result.contains("Vault body text."),
            "expected document body, got: {result}"
        );
    }

    #[test]
    fn execute_rejects_unknown_tool() {
        let temp = tempfile::tempdir().expect("tempdir");
        let executor = executor(temp.path());
        let call = ToolCall {
            id: "call-x".to_string(),
            name: "delete_everything".to_string(),
            arguments: Value::Null,
        };
        let error = executor.execute(&call).expect_err("unknown tool rejected");
        assert!(error.message.contains("unknown tool"));
    }

    /// Recorded tool-calling transport: turn 1 asks for a vault tool, turn 2
    /// returns the final narrative. Exercises the gwiki tool-loop path end-to-end
    /// over the real `VaultToolExecutor`.
    struct ScriptedChatTransport {
        completions: RefCell<VecDeque<ChatCompletion>>,
    }

    impl ScriptedChatTransport {
        fn new(completions: Vec<ChatCompletion>) -> Self {
            Self {
                completions: RefCell::new(completions.into()),
            }
        }
    }

    impl ChatTransport for ScriptedChatTransport {
        fn complete(&self, _request: ChatCompletionRequest<'_>) -> Result<ChatCompletion, AiError> {
            Ok(self
                .completions
                .borrow_mut()
                .pop_front()
                .expect("scripted completion available"))
        }

        fn route(&self) -> &'static str {
            "stub"
        }
    }

    #[test]
    fn tool_loop_invokes_vault_tool_then_completes() {
        let temp = tempfile::tempdir().expect("tempdir");
        let path = write_vault_doc(
            temp.path(),
            "# Overview\n\nGrounding fact the model retrieved.",
        );
        let executor = executor(temp.path());

        let read_call = ToolCall {
            id: "call-1".to_string(),
            name: "read_document".to_string(),
            arguments: json!({ "path": path }),
        };
        let transport = ScriptedChatTransport::new(vec![
            ChatCompletion {
                tool_calls: vec![read_call],
                finish_reason: Some("tool_calls".to_string()),
                ..Default::default()
            },
            ChatCompletion {
                content: Some("Final grounded narrative.".to_string()),
                finish_reason: Some("stop".to_string()),
                ..Default::default()
            },
        ]);

        let messages = vec![
            ChatMessage::system("Investigate the vault, then write."),
            ChatMessage::user("Compile a page."),
        ];
        let limits = ToolLoopLimits::default();
        let outcome = run_tool_loop(
            &transport,
            std::sync::Arc::new(executor),
            messages,
            &limits,
            None,
        )
        .expect("tool loop runs");

        assert!(outcome.stop_reason.is_completed());
        assert_eq!(
            outcome.content.as_deref(),
            Some("Final grounded narrative.")
        );
        assert_eq!(outcome.observability.tool_call_count, 1);
        assert!(
            outcome
                .observability
                .tool_names
                .contains(&"read_document".to_string())
        );
    }
}
