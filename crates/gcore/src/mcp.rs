//! Internal MCP family contract: immutable tool descriptors and schema identity.
//!
//! A family crate implements [`ToolFamily`] and returns one [`ToolServer`]
//! descriptor without depending on gdaemon. gdaemon owns the [`ToolRegistry`]
//! instance and supplies a [`ToolContext`] at call time.

// TODO(#24883): behavior-free stub for the RED test commit; the Developer
// implements it against `tests/mcp_contract.rs`.

use std::future::Future;
use std::pin::Pin;

use schemars::JsonSchema;
use serde::de::DeserializeOwned;
use serde_json::Value;

/// Caller context resolved by the gateway. Never part of a tool's input schema.
#[derive(Debug, Clone, Default)]
pub struct ToolContext {
    pub session_id: Option<String>,
    pub project_id: Option<String>,
    pub principal: Option<String>,
    pub agent_run_id: Option<String>,
    pub machine_id: Option<String>,
    pub intent: Option<String>,
    pub origin: Option<String>,
}

/// A tool call failure.
#[derive(Debug, thiserror::Error)]
pub enum ToolError {
    #[error("unknown tool {server}/{tool}")]
    UnknownTool { server: String, tool: String },
    #[error("invalid arguments: {0}")]
    InvalidArguments(String),
    #[error("{code}: {message}")]
    Failed { code: String, message: String },
}

/// A descriptor invariant violated at registry construction.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum RegistryError {
    #[error("duplicate server name {server}")]
    DuplicateServer { server: String },
    #[error("duplicate tool name {server}/{tool}")]
    DuplicateTool { server: String, tool: String },
    #[error("invalid public name {name:?}")]
    InvalidName { name: String },
    #[error("input schema for {server}/{tool} exposes ToolContext")]
    ContextInSchema { server: String, tool: String },
}

/// The boxed future a tool handler returns.
pub type ToolFuture = Pin<Box<dyn Future<Output = Result<Value, ToolError>> + Send>>;

/// One tool: metadata plus a typed async handler.
pub struct ToolDefinition {
    _private: (),
}

impl ToolDefinition {
    /// A tool whose input schema derives from `Req`.
    pub fn new<Req, F, Fut>(_name: &'static str, _description: &'static str, _handler: F) -> Self
    where
        Req: DeserializeOwned + JsonSchema + Send + 'static,
        F: Fn(ToolContext, Req) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<Value, ToolError>> + Send + 'static,
    {
        todo!("ToolDefinition::new")
    }

    /// Custom brief text for discovery listings.
    pub fn brief(self, _brief: &'static str) -> Self {
        todo!("ToolDefinition::brief")
    }

    /// Output schema derived from `Out`.
    pub fn output_schema<Out: JsonSchema>(self) -> Self {
        todo!("ToolDefinition::output_schema")
    }

    /// Whether the handler performs no externally visible mutation.
    pub fn read_only(self, _read_only: bool) -> Self {
        todo!("ToolDefinition::read_only")
    }
}

/// One immutable public server descriptor.
pub struct ToolServer {
    _private: (),
}

impl ToolServer {
    pub fn new(
        _name: &'static str,
        _description: &'static str,
        _tools: Vec<ToolDefinition>,
    ) -> Self {
        todo!("ToolServer::new")
    }
}

/// A family crate's internal MCP surface.
pub trait ToolFamily: Send + Sync {
    /// The family's one public server descriptor.
    fn server(&self) -> ToolServer;
}

/// A validated tool with its normalized schema and cached definition hash.
pub struct RegisteredTool {
    _private: (),
}

impl RegisteredTool {
    pub fn name(&self) -> &str {
        todo!("RegisteredTool::name")
    }
    pub fn description(&self) -> &str {
        todo!("RegisteredTool::description")
    }
    pub fn brief(&self) -> Option<&str> {
        todo!("RegisteredTool::brief")
    }
    pub fn input_schema(&self) -> &Value {
        todo!("RegisteredTool::input_schema")
    }
    pub fn output_schema(&self) -> Option<&Value> {
        todo!("RegisteredTool::output_schema")
    }
    pub fn read_only(&self) -> bool {
        todo!("RegisteredTool::read_only")
    }
    pub fn definition_hash(&self) -> &str {
        todo!("RegisteredTool::definition_hash")
    }
}

/// Every family's validated descriptors, built once at startup.
pub struct ToolRegistry {
    _private: (),
}

impl ToolRegistry {
    pub fn new(_families: Vec<Box<dyn ToolFamily>>) -> Result<Self, RegistryError> {
        todo!("ToolRegistry::new")
    }

    pub fn tool(&self, _server: &str, _tool: &str) -> Option<&RegisteredTool> {
        todo!("ToolRegistry::tool")
    }

    /// Validate `arguments` against the input schema, then deserialize and dispatch.
    pub async fn call(
        &self,
        _server: &str,
        _tool: &str,
        _context: ToolContext,
        _arguments: Value,
    ) -> Result<Value, ToolError> {
        todo!("ToolRegistry::call")
    }
}

/// Canonical `{description,input_schema}` JSON, byte-compatible with Python's
/// `json.dumps(sort_keys=True, separators=(",", ":"))`.
pub fn canonical_definition_json(_description: &str, _input_schema: &Value) -> String {
    todo!("canonical_definition_json")
}

/// 16-hex SHA-256 prefix of [`canonical_definition_json`], matching
/// `gobby.mcp_proxy.schema_hash.compute_schema_hash`.
pub fn definition_hash(_description: &str, _input_schema: &Value) -> String {
    todo!("definition_hash")
}
