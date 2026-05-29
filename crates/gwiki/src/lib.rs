use std::fmt;
use std::path::PathBuf;

pub mod audit;
pub mod citations;
pub mod collect;
pub mod commands;
pub mod compile;
pub mod credibility;
pub mod daemon;
pub mod events;
pub mod exports;
pub mod frontmatter;
pub mod graph;
pub mod health;
pub mod indexer;
pub mod ingest;
pub mod links;
pub mod lint;
pub mod log;
pub mod markdown;
pub mod models;
pub mod output;
pub mod provenance;
pub mod registry;
pub mod research;
pub mod schema;
pub mod scope;
pub mod search;
pub mod session;
pub mod setup;
pub mod sources;
pub mod store;
pub mod synthesis;
pub mod transcribe;
pub mod vault;
pub mod vision;

/// Parsed gwiki command passed in from the binary.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Command {
    Init {
        scope: ScopeSelection,
    },
    Setup {
        scope: ScopeSelection,
    },
    Index {
        scope: ScopeSelection,
    },
    Collect {
        scope: ScopeSelection,
    },
    IngestFile {
        path: PathBuf,
        scope: ScopeSelection,
    },
    Search {
        query: String,
        scope: ScopeSelection,
        limit: usize,
    },
    Backlinks {
        page: String,
        scope: ScopeSelection,
    },
    LinkSuggest {
        scope: ScopeSelection,
        limit: usize,
    },
    Research(research::ResearchOptions),
    Compile {
        topic: Option<String>,
        outline: Vec<String>,
        target_kind: synthesis::ArticleKind,
        target_page: Option<PathBuf>,
        write_intent: bool,
        scope: ScopeSelection,
    },
    Export {
        scope: ScopeSelection,
        command: exports::ExportCommand,
    },
    Audit {
        scope: ScopeSelection,
    },
    Lint {
        scope: ScopeSelection,
    },
    Health {
        scope: ScopeSelection,
    },
    Status {
        scope: ScopeSelection,
    },
}

/// Shared scope flags accepted by shell commands.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ScopeSelection {
    pub project: bool,
    pub topic: Option<String>,
}

impl ScopeSelection {
    pub fn project() -> Self {
        Self {
            project: true,
            topic: None,
        }
    }

    pub fn topic(topic: impl Into<String>) -> Self {
        Self {
            project: false,
            topic: Some(topic.into()),
        }
    }

    pub fn identity(&self) -> ScopeIdentity {
        if let Some(topic) = &self.topic {
            return ScopeIdentity::topic(topic.clone());
        }

        ScopeIdentity::project("current")
    }
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct ScopeIdentity {
    pub kind: String,
    pub id: String,
}

impl ScopeIdentity {
    pub fn project(id: impl Into<String>) -> Self {
        Self {
            kind: "project".to_string(),
            id: id.into(),
        }
    }

    pub fn topic(id: impl Into<String>) -> Self {
        Self {
            kind: "topic".to_string(),
            id: id.into(),
        }
    }
}

impl fmt::Display for ScopeIdentity {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}:{}", self.kind, self.id)
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct CommandOutcome {
    pub status_messages: Vec<String>,
    pub result: CommandResult,
}

#[derive(Debug, Clone, PartialEq)]
pub struct CommandResult {
    pub payload: serde_json::Value,
    pub text: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WikiError {
    NotImplemented {
        command: &'static str,
        detail: &'static str,
    },
    InvalidScope {
        detail: String,
    },
    Config {
        detail: String,
    },
    Io {
        action: &'static str,
        path: Option<PathBuf>,
        source: String,
    },
    Json {
        action: &'static str,
        path: Option<PathBuf>,
        source: String,
    },
    Registry {
        detail: String,
    },
    Daemon {
        endpoint: &'static str,
        message: String,
    },
    InvalidInput {
        field: &'static str,
        message: String,
    },
}

impl WikiError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::NotImplemented { .. } => "not_implemented",
            Self::InvalidScope { .. } => "invalid_scope",
            Self::Config { .. } => "config_error",
            Self::Io { .. } => "io_error",
            Self::Json { .. } => "json_error",
            Self::Registry { .. } => "registry_error",
            Self::Daemon { .. } => "daemon_error",
            Self::InvalidInput { .. } => "invalid_input",
        }
    }
}

impl fmt::Display for WikiError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotImplemented { command, detail } => {
                write!(f, "{command}: {detail} ({})", self.code())
            }
            Self::InvalidScope { detail } | Self::Config { detail } | Self::Registry { detail } => {
                write!(f, "{detail} ({})", self.code())
            }
            Self::Io {
                action,
                path,
                source,
            }
            | Self::Json {
                action,
                path,
                source,
            } => match path {
                Some(path) => write!(
                    f,
                    "{action} failed for {}: {source} ({})",
                    path.display(),
                    self.code()
                ),
                None => write!(f, "{action} failed: {source} ({})", self.code()),
            },
            Self::Daemon { endpoint, message } => {
                write!(f, "{endpoint}: {message} ({})", self.code())
            }
            Self::InvalidInput { field, message } => {
                write!(f, "{field}: {message} ({})", self.code())
            }
        }
    }
}

impl std::error::Error for WikiError {}

pub fn run(command: Command) -> Result<CommandOutcome, WikiError> {
    match command {
        Command::Init { scope } => init(scope),
        Command::Setup { scope } => Ok(commands::setup::run(scope.identity())),
        Command::Index { scope } => Ok(commands::index::run(scope.identity())),
        Command::Collect { scope } => collect(scope),
        Command::IngestFile { path, scope } => {
            Ok(commands::index::ingest_file(path, scope.identity()))
        }
        Command::Search {
            query,
            scope,
            limit,
        } => Ok(commands::search::run(query, scope.identity(), limit)),
        Command::Backlinks { page, scope } => Ok(commands::backlinks::run(page, scope.identity())),
        Command::LinkSuggest { scope, limit } => {
            Ok(commands::backlinks::link_suggest(scope.identity(), limit))
        }
        Command::Research(options) => run_research(options),
        Command::Compile {
            topic,
            outline,
            target_kind,
            target_page,
            write_intent,
            scope,
        } => run_compile(
            topic,
            outline,
            target_kind,
            target_page,
            write_intent,
            scope,
        ),
        Command::Export { scope, command } => run_export(scope, command),
        Command::Audit { scope } => run_audit(scope),
        Command::Lint { scope } => run_lint(scope),
        Command::Health { scope } => run_health(scope),
        Command::Status { scope } => Ok(commands::status::run(scope.identity())),
    }
}

fn collect(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let cwd = std::env::current_dir().map_err(|error| WikiError::Io {
        action: "read current directory",
        path: None,
        source: error.to_string(),
    })?;
    let scope = scope::resolve(&selection, &cwd)?;

    vault::initialize(&scope)?;
    let output_scope = resolved_scope_identity(&scope);
    let report = collect::collect_inbox(scope.root(), &collect_timestamp())?;
    Ok(commands::collect::run(output_scope, scope.root(), report))
}

fn run_research(options: research::ResearchOptions) -> Result<CommandOutcome, WikiError> {
    let outcome = research::run(options)?;
    let session = outcome.session;
    let message = outcome.message;
    let output_scope = research_scope_identity(&session.scope);
    let payload = serde_json::json!({
        "command": "research",
        "scope": output_scope,
        "status": "checkpointed",
        "session": session,
    });

    Ok(commands::scoped_outcome(
        "research",
        &output_scope,
        payload,
        message,
    ))
}

fn run_compile(
    topic: Option<String>,
    outline: Vec<String>,
    target_kind: synthesis::ArticleKind,
    target_page: Option<PathBuf>,
    write_intent: bool,
    scope: ScopeSelection,
) -> Result<CommandOutcome, WikiError> {
    let research_scope = research::resolve_scope(&scope)?;
    let mut session = session::ResearchSession::load_checkpoint(research_scope.root())?;
    let topic = topic.unwrap_or_else(|| {
        session
            .compile_state
            .as_ref()
            .map(|state| state.topic.clone())
            .unwrap_or_else(|| session.question.clone())
    });
    let target_page = target_page.map(|path| {
        if path.is_absolute() {
            path
        } else {
            research_scope.root().join(path)
        }
    });
    let daemon_report = daemon::probe_daemon_capabilities();
    let outcome = compile::compile_to_wiki_with_options(
        &mut session,
        compile::CompileRequest {
            topic,
            outline,
            target_page,
            write_intent,
        },
        compile::WikiCompileOptions {
            target_kind,
            daemon_synthesis_available: daemon_report.synthesis.available,
        },
    )?;
    let output_scope = research_scope_identity(&session.scope);
    let payload = serde_json::json!({
        "command": "compile",
        "scope": output_scope,
        "status": "compiled",
        "daemon_synthesis_available": daemon_report.synthesis.available,
        "article_path": outcome.article_path,
        "source_paths": outcome.source_paths,
        "index_path": outcome.index_path,
        "handoff_id": outcome.handoff_id,
        "page_writes": outcome.page_writes,
        "prompt": outcome.prompt,
    });
    let text = format!(
        "Compiled wiki article\nScope: {output_scope}\nArticle: {}",
        outcome.article_path.display()
    );
    Ok(commands::scoped_outcome(
        "compile",
        &output_scope,
        payload,
        text,
    ))
}

fn run_export(
    selection: ScopeSelection,
    command: exports::ExportCommand,
) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    let output_scope = resolved_scope_identity(&scope);
    let artifacts = exports::run(scope.root(), command)?;
    let output = exports::ExportOutput::new(output_scope.clone(), artifacts);
    let payload = serde_json::to_value(&output).map_err(|error| WikiError::Json {
        action: "serialize export output",
        path: None,
        source: error.to_string(),
    })?;
    let paths = output
        .artifacts
        .iter()
        .map(|artifact| artifact.path.display().to_string())
        .collect::<Vec<_>>()
        .join(", ");
    let text = format!("Exported wiki artifacts\nScope: {output_scope}\nArtifacts: {paths}");
    Ok(commands::scoped_outcome(
        "export",
        &output_scope,
        payload,
        text,
    ))
}

fn run_audit(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    let output_scope = resolved_scope_identity(&scope);
    let report = audit::run(scope.root(), output_scope.clone())?;
    let payload = serde_json::to_value(&report).map_err(|error| WikiError::Json {
        action: "serialize audit report",
        path: None,
        source: error.to_string(),
    })?;
    Ok(commands::scoped_outcome(
        "audit",
        &output_scope,
        payload,
        audit::render_text(&report),
    ))
}

fn run_lint(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    let output_scope = resolved_scope_identity(&scope);
    let report = lint::run(scope.root(), output_scope.clone())?;
    let payload = serde_json::to_value(&report).map_err(|error| WikiError::Json {
        action: "serialize lint report",
        path: None,
        source: error.to_string(),
    })?;
    Ok(commands::scoped_outcome(
        "lint",
        &output_scope,
        payload,
        lint::render_text(&report),
    ))
}

fn run_health(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    let output_scope = resolved_scope_identity(&scope);
    let report = health::run(scope.root(), output_scope.clone())?;
    let payload = serde_json::to_value(&report).map_err(|error| WikiError::Json {
        action: "serialize health report",
        path: None,
        source: error.to_string(),
    })?;
    Ok(commands::scoped_outcome(
        "health",
        &output_scope,
        payload,
        health::render_text(&report),
    ))
}

fn resolve_command_scope(selection: &ScopeSelection) -> Result<scope::ResolvedScope, WikiError> {
    let cwd = std::env::current_dir().map_err(|error| WikiError::Io {
        action: "read current directory",
        path: None,
        source: error.to_string(),
    })?;
    scope::resolve(selection, &cwd)
}

fn research_scope_identity(scope: &session::ResearchScope) -> ScopeIdentity {
    match scope {
        session::ResearchScope::Project { .. } => ScopeIdentity::project("current"),
        session::ResearchScope::Topic { name, .. } => ScopeIdentity::topic(name.clone()),
    }
}

fn init(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let cwd = std::env::current_dir().map_err(|error| WikiError::Io {
        action: "read current directory",
        path: None,
        source: error.to_string(),
    })?;
    let scope = scope::resolve(&selection, &cwd)?;

    vault::initialize(&scope)?;
    registry::register_scope(scope.registry_path(), &scope)?;

    let output_scope = resolved_scope_identity(&scope);
    let required_paths = vault::required_paths();
    let payload = serde_json::json!({
        "command": "init",
        "scope": output_scope,
        "status": "ready",
        "root": scope.root(),
        "created": {
            "directories": required_paths.directories,
            "files": required_paths.files,
        },
    });
    let text = format!(
        "Initialized wiki\nScope: {output_scope}\nRoot: {}",
        scope.root().display()
    );
    Ok(commands::scoped_outcome(
        "init",
        &output_scope,
        payload,
        text,
    ))
}

fn resolved_scope_identity(scope: &scope::ResolvedScope) -> ScopeIdentity {
    if let Some(topic) = scope.topic_name() {
        return ScopeIdentity::topic(topic);
    }

    if let Some(project_id) = scope.project_id() {
        return ScopeIdentity::project(project_id);
    }

    ScopeIdentity::project("current")
}

fn collect_timestamp() -> String {
    let millis = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_millis())
        .unwrap_or_default();
    format!("unix-ms:{millis}")
}

#[cfg(test)]
mod lib {
    mod tests {
        #[test]
        fn crate_has_no_gcode_dependency() {
            let manifest =
                std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/Cargo.toml"))
                    .expect("manifest is readable");
            let manifest: toml::Value = toml::from_str(&manifest).expect("manifest is valid TOML");
            let dependencies = manifest
                .get("dependencies")
                .and_then(toml::Value::as_table)
                .expect("manifest has dependencies table");

            assert!(
                dependencies.contains_key("gobby-core"),
                "gobby-wiki must depend on gobby-core"
            );
            assert!(
                !dependencies.contains_key("gobby-code"),
                "gobby-wiki must not depend on gobby-code"
            );
        }
    }
}
