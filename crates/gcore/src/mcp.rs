//! Internal MCP family contract: immutable tool descriptors and schema identity.
//!
//! Family crates depend inward on this module. The hub gateway owns registry
//! composition, caller authentication, policy, and backend selection.

use std::collections::BTreeMap;
use std::fmt::Write;
use std::future::Future;
use std::pin::Pin;

use jsonschema::{Draft, JSONSchema};
use schemars::{JsonSchema, generate::SchemaSettings};
use serde::de::DeserializeOwned;
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::json::{CANONICAL, Json, dumps};

/// Authenticated caller context supplied by the gateway, never serialized input.
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

#[derive(Debug, thiserror::Error)]
pub enum ToolError {
    #[error("unknown tool {server}/{tool}")]
    UnknownTool { server: String, tool: String },
    #[error("invalid arguments: {0}")]
    InvalidArguments(String),
    #[error("{code}: {message}")]
    Failed { code: String, message: String },
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum RegistryError {
    #[error("duplicate server name {server}")]
    DuplicateServer { server: String },
    #[error("duplicate tool name {server}/{tool}")]
    DuplicateTool { server: String, tool: String },
    #[error("invalid public name {name:?}")]
    InvalidName { name: String },
    #[error("schema for {server}/{tool} exposes ToolContext")]
    ContextInSchema { server: String, tool: String },
    #[error("invalid schema for {server}/{tool}: {message}")]
    InvalidSchema {
        server: String,
        tool: String,
        message: String,
    },
}

pub type ToolFuture = Pin<Box<dyn Future<Output = Result<Value, ToolError>> + Send>>;
type Handler = Box<dyn Fn(ToolContext, Value) -> ToolFuture + Send + Sync>;

/// Builder consumed at startup; registered descriptors have no mutable access.
pub struct ToolDefinition {
    name: &'static str,
    description: &'static str,
    brief: Option<&'static str>,
    input_schema: Value,
    output_schema: Option<Value>,
    context_in_schema: bool,
    read_only: bool,
    handler: Handler,
}

impl ToolDefinition {
    pub fn new<Req, F, Fut>(name: &'static str, description: &'static str, handler: F) -> Self
    where
        Req: DeserializeOwned + JsonSchema + Send + 'static,
        F: Fn(ToolContext, Req) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<Value, ToolError>> + Send + 'static,
    {
        let (input_schema, context_in_schema) = normalized_schema::<Req>(SchemaSettings::draft07());
        Self {
            name,
            description,
            brief: None,
            input_schema,
            output_schema: None,
            context_in_schema,
            read_only: false,
            handler: Box::new(move |ctx, arguments| {
                // The registry validates first. Serde can still reject custom
                // constraints that JSON Schema cannot express.
                match serde_json::from_value::<Req>(arguments) {
                    Ok(request) => Box::pin(handler(ctx, request)),
                    Err(error) => {
                        Box::pin(async move { Err(ToolError::InvalidArguments(error.to_string())) })
                    }
                }
            }),
        }
    }

    pub fn brief(mut self, brief: &'static str) -> Self {
        self.brief = Some(brief);
        self
    }

    pub fn output_schema<Out: JsonSchema>(mut self) -> Self {
        let (schema, has_context) =
            normalized_schema::<Out>(SchemaSettings::draft07().for_serialize());
        self.output_schema = Some(schema);
        self.context_in_schema |= has_context;
        self
    }

    pub fn read_only(mut self, read_only: bool) -> Self {
        self.read_only = read_only;
        self
    }
}

/// One public server exported by a family crate.
pub struct ToolServer {
    name: &'static str,
    description: &'static str,
    tools: Vec<ToolDefinition>,
}

impl ToolServer {
    pub fn new(name: &'static str, description: &'static str, tools: Vec<ToolDefinition>) -> Self {
        Self {
            name,
            description,
            tools,
        }
    }
}

pub trait ToolFamily: Send + Sync {
    fn server(&self) -> ToolServer;
}

/// Discovery, validation and dispatch share this immutable definition.
pub struct RegisteredTool {
    definition: ToolDefinition,
    validator: JSONSchema,
    canonical_json: String,
    definition_hash: String,
}

impl RegisteredTool {
    pub fn name(&self) -> &str {
        self.definition.name
    }
    pub fn description(&self) -> &str {
        self.definition.description
    }
    pub fn brief(&self) -> Option<&str> {
        self.definition.brief
    }
    pub fn input_schema(&self) -> &Value {
        &self.definition.input_schema
    }
    pub fn output_schema(&self) -> Option<&Value> {
        self.definition.output_schema.as_ref()
    }
    pub fn read_only(&self) -> bool {
        self.definition.read_only
    }
    pub fn definition_hash(&self) -> &str {
        &self.definition_hash
    }
    pub fn canonical_json(&self) -> &str {
        &self.canonical_json
    }
}

struct RegisteredServer {
    description: &'static str,
    tools: BTreeMap<&'static str, RegisteredTool>,
}

pub struct ToolRegistry {
    servers: BTreeMap<&'static str, RegisteredServer>,
}

impl ToolRegistry {
    pub fn new(families: Vec<Box<dyn ToolFamily>>) -> Result<Self, RegistryError> {
        let mut servers = BTreeMap::new();
        for family in families {
            let server = family.server();
            validate_name(server.name)?;
            if servers.contains_key(server.name) {
                return Err(RegistryError::DuplicateServer {
                    server: server.name.into(),
                });
            }
            let mut tools = BTreeMap::new();
            for definition in server.tools {
                validate_name(definition.name)?;
                if tools.contains_key(definition.name) {
                    return Err(RegistryError::DuplicateTool {
                        server: server.name.into(),
                        tool: definition.name.into(),
                    });
                }
                if definition.context_in_schema {
                    return Err(RegistryError::ContextInSchema {
                        server: server.name.into(),
                        tool: definition.name.into(),
                    });
                }
                let validator = JSONSchema::options()
                    .with_draft(Draft::Draft7)
                    .compile(&definition.input_schema)
                    .map_err(|error| RegistryError::InvalidSchema {
                        server: server.name.into(),
                        tool: definition.name.into(),
                        message: error.to_string(),
                    })?;
                let canonical_json =
                    canonical_definition_json(definition.description, &definition.input_schema);
                let definition_hash = hash_canonical(&canonical_json);
                tools.insert(
                    definition.name,
                    RegisteredTool {
                        definition,
                        validator,
                        canonical_json,
                        definition_hash,
                    },
                );
            }
            servers.insert(
                server.name,
                RegisteredServer {
                    description: server.description,
                    tools,
                },
            );
        }
        Ok(Self { servers })
    }

    /// Stable public-name order for discovery.
    pub fn servers(&self) -> impl Iterator<Item = (&str, &str)> {
        self.servers
            .iter()
            .map(|(name, server)| (*name, server.description))
    }

    pub fn tools(&self, server: &str) -> impl Iterator<Item = &RegisteredTool> {
        self.servers
            .get(server)
            .into_iter()
            .flat_map(|server| server.tools.values())
    }

    pub fn tool(&self, server: &str, tool: &str) -> Option<&RegisteredTool> {
        self.servers.get(server)?.tools.get(tool)
    }

    pub async fn call(
        &self,
        server: &str,
        tool: &str,
        context: ToolContext,
        arguments: Value,
    ) -> Result<Value, ToolError> {
        let registered = self
            .tool(server, tool)
            .ok_or_else(|| ToolError::UnknownTool {
                server: server.into(),
                tool: tool.into(),
            })?;
        if let Err(errors) = registered.validator.validate(&arguments) {
            let message = errors
                .map(|error| format!("{}: {error}", error.instance_path))
                .collect::<Vec<_>>()
                .join("; ");
            return Err(ToolError::InvalidArguments(format!(
                "{server}/{tool}: {message}"
            )));
        }
        (registered.definition.handler)(context, arguments).await
    }
}

fn validate_name(name: &str) -> Result<(), RegistryError> {
    if name.is_empty()
        || name
            .chars()
            .any(|c| c.is_whitespace() || c.is_control() || c == '/')
    {
        return Err(RegistryError::InvalidName { name: name.into() });
    }
    Ok(())
}

fn normalized_schema<T: JsonSchema>(settings: SchemaSettings) -> (Value, bool) {
    // Draft 7 is supported by the startup validator, including local refs.
    let mut schema = settings
        .into_generator()
        .into_root_schema_for::<T>()
        .to_value();
    let context = exposes_context(&schema);
    if let Some(object) = schema.as_object_mut() {
        object.remove("$schema");
        object.remove("title");
    }
    (schema, context)
}

fn exposes_context(value: &Value) -> bool {
    match value {
        Value::Object(object) => {
            object.get("title").and_then(Value::as_str) == Some("ToolContext")
                || object
                    .get("$ref")
                    .and_then(Value::as_str)
                    .is_some_and(|reference| reference.ends_with("/ToolContext"))
                || ["$defs", "definitions"].iter().any(|key| {
                    object
                        .get(*key)
                        .and_then(Value::as_object)
                        .is_some_and(|definitions| definitions.contains_key("ToolContext"))
                })
                || object.values().any(exposes_context)
        }
        Value::Array(values) => values.iter().any(exposes_context),
        _ => false,
    }
}

/// Python json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True).
pub fn canonical_definition_json(description: &str, input_schema: &Value) -> String {
    let definition = Json::Object(vec![
        ("description".into(), Json::Str(description.into())),
        ("input_schema".into(), Json::from(input_schema)),
    ]);
    dumps(&definition, CANONICAL).expect("CANONICAL writes every float a Value can hold")
}

pub fn definition_hash(description: &str, input_schema: &Value) -> String {
    hash_canonical(&canonical_definition_json(description, input_schema))
}

fn hash_canonical(canonical: &str) -> String {
    let digest = Sha256::digest(canonical.as_bytes());
    let mut hash = String::with_capacity(16);
    for byte in &digest[..8] {
        write!(hash, "{byte:02x}").expect("writing to String is infallible");
    }
    hash
}
