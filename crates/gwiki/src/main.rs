use std::path::PathBuf;
use std::process::ExitCode;

use clap::{ArgGroup, Args, Parser, Subcommand, ValueEnum};
use gobby_core::config::AiRouting;
use gobby_wiki::{Command, IngestFileOptions, ReadTarget, ScopeSelection, WikiError, output};
use serde_json::json;

#[derive(Debug, Parser)]
#[command(name = "gwiki", version, about = "Gobby wiki CLI")]
struct Cli {
    #[command(flatten)]
    scope: ScopeArgs,

    /// Output format.
    #[arg(long, global = true, default_value = "json")]
    format: output::Format,

    /// Suppress status messages.
    #[arg(long, global = true)]
    quiet: bool,

    #[command(subcommand)]
    command: CliCommand,
}

#[derive(Debug, Subcommand)]
enum CliCommand {
    /// Initialize a wiki vault.
    Init,
    /// Create gwiki-owned derived storage.
    Setup(SetupArgs),
    /// Index markdown and source notes in the selected scope.
    Index,
    /// Collect recognized inbox drops into raw storage.
    Collect,
    /// Capture a local source file into the wiki inbox.
    IngestFile {
        #[arg(value_name = "PATH")]
        path: PathBuf,
        /// Disable AI-backed media extraction for this ingest.
        #[arg(long)]
        no_ai: bool,
        /// Prefer audio translation over transcription where a backend is available.
        #[arg(long)]
        translate: bool,
        /// Target language for audio translation.
        #[arg(long, value_name = "LANG")]
        target_lang: Option<String>,
        /// Seconds between sampled video frames; 0 disables frames.
        #[arg(long = "video-frame-interval", value_name = "SECONDS")]
        video_frame_interval_seconds: Option<u32>,
        /// Routing override for audio transcription and translation.
        #[arg(long, value_name = "auto|daemon|direct|off")]
        transcription_routing: Option<AiRouting>,
        /// Routing override for vision extraction.
        #[arg(long, value_name = "auto|daemon|direct|off")]
        vision_routing: Option<AiRouting>,
        /// Routing override for text generation.
        #[arg(long, value_name = "auto|daemon|direct|off")]
        text_routing: Option<AiRouting>,
    },
    /// Search wiki documents in the selected scope.
    Search(SearchArgs),
    /// Read a wiki page or document in the selected scope.
    Read(ReadArgs),
    /// Show backlinks for a wiki page.
    Backlinks(BacklinksArgs),
    /// Suggest unresolved wiki links in the selected scope.
    LinkSuggest(LinkSuggestArgs),
    /// Dispatch research workers and checkpoint wiki research state.
    Research(ResearchArgs),
    /// Compile accepted research notes into wiki articles.
    Compile(CompileArgs),
    /// Export generated bundles and reports under outputs/.
    Export(ExportArgs),
    /// Report claims that lack source support.
    Audit,
    /// Detect broken links and vault hygiene issues.
    Lint,
    /// Write wiki health snapshots under meta/health.
    Health,
    /// Show shell readiness.
    Status,
}

#[derive(Debug, Args)]
struct ScopeArgs {
    /// Use the current Gobby project's wiki scope; default is global.
    #[arg(long, global = true, conflicts_with = "topic")]
    project: bool,

    /// Use a named topic wiki scope; default is global.
    #[arg(long, global = true, value_name = "NAME")]
    topic: Option<String>,
}

#[derive(Debug, Args)]
struct SetupArgs {
    /// Resolve or provision the shared Gobby hub before creating gwiki storage.
    #[arg(long)]
    standalone: bool,

    /// PostgreSQL URL to use for setup without persisting the flag value in output.
    #[arg(long = "database-url", value_name = "DSN")]
    database_url: Option<String>,

    /// Do not provision Docker services when no reachable hub is configured.
    #[arg(long)]
    no_services: bool,

    #[arg(long, value_name = "HOST")]
    falkordb_host: Option<String>,

    #[arg(long, value_name = "PORT")]
    falkordb_port: Option<u16>,

    #[arg(long, value_name = "PASSWORD")]
    falkordb_password: Option<String>,

    #[arg(long, value_name = "URL")]
    qdrant_url: Option<String>,

    #[arg(long, value_name = "PROVIDER")]
    embedding_provider: Option<String>,

    #[arg(long, value_name = "URL")]
    embedding_api_base: Option<String>,

    #[arg(long, value_name = "MODEL")]
    embedding_model: Option<String>,

    #[arg(long, value_name = "PREFIX")]
    embedding_query_prefix: Option<String>,

    #[arg(long, value_name = "DIM")]
    embedding_vector_dim: Option<usize>,

    #[arg(long, value_name = "KEY")]
    embedding_api_key: Option<String>,
}

#[derive(Debug, Args)]
struct SearchArgs {
    #[arg(value_name = "QUERY")]
    query: String,

    #[arg(long, default_value = "10")]
    limit: usize,

    /// Disable semantic vector search for this query.
    #[arg(long = "no-semantic")]
    no_semantic: bool,
}

#[derive(Debug, Args)]
#[command(group(
    ArgGroup::new("target")
        .required(true)
        .args(["path", "title"])
))]
struct ReadArgs {
    /// Vault-relative wiki path to read.
    #[arg(long, value_name = "PATH")]
    path: Option<PathBuf>,

    /// First-heading title to resolve inside the selected scope.
    #[arg(long, value_name = "TITLE")]
    title: Option<String>,
}

#[derive(Debug, Args)]
struct BacklinksArgs {
    #[arg(value_name = "PAGE")]
    page: String,
}

#[derive(Debug, Args)]
struct LinkSuggestArgs {
    #[arg(long, default_value = "10")]
    limit: usize,
}

#[derive(Debug, Args)]
struct ResearchArgs {
    #[arg(value_name = "QUESTION")]
    question: Option<String>,

    #[arg(long = "source-constraint", value_name = "TEXT")]
    source_constraints: Vec<String>,

    #[arg(long, default_value_t = 3, value_name = "N")]
    agent_count: usize,

    #[arg(long, value_name = "TASK")]
    task_id: Option<String>,

    #[arg(long)]
    resume: bool,
}

#[derive(Debug, Args)]
struct CompileArgs {
    #[arg(value_name = "TOPIC")]
    topic: Option<String>,

    #[arg(long = "outline", value_name = "HEADING")]
    outline: Vec<String>,

    #[arg(long, value_enum, default_value = "topic")]
    kind: CompileKind,

    #[arg(long, value_name = "PAGE")]
    target: Option<PathBuf>,

    #[arg(long = "write-intent")]
    write_intent: bool,
}

#[derive(Debug, Args)]
struct ExportArgs {
    #[command(subcommand)]
    command: ExportSubcommand,
}

#[derive(Debug, Subcommand)]
enum ExportSubcommand {
    /// Export bundled workflow prompts and skill assets.
    WorkflowAssets {
        #[arg(long, default_value = "workflow-assets.md", value_name = "FILE")]
        output: String,
    },
    /// Export an existing generated report file.
    Report {
        #[arg(long, value_name = "FILE")]
        output: String,

        #[arg(long = "from", value_name = "PATH")]
        source: PathBuf,
    },
}

#[derive(Clone, Copy, Debug, ValueEnum)]
enum CompileKind {
    Source,
    Concept,
    Topic,
}

fn main() -> ExitCode {
    let Cli {
        scope,
        format,
        quiet,
        command,
    } = Cli::parse();

    let command = match command_from_cli(command, scope.into()) {
        Ok(command) => command,
        Err(error) => {
            print_error(format, &error);
            return exit_code_for_error(&error);
        }
    };

    match gobby_wiki::run(command) {
        Ok(outcome) => {
            if !quiet {
                for message in &outcome.status_messages {
                    output::print_status(message);
                }
            }

            let stdout = std::io::stdout().lock();
            if let Err(error) = output::print_result(stdout, format, &outcome.result) {
                eprintln!("gwiki: {error}");
                return ExitCode::from(1);
            }
            ExitCode::SUCCESS
        }
        Err(error) => {
            print_error(format, &error);
            exit_code_for_error(&error)
        }
    }
}

fn print_error(format: output::Format, error: &WikiError) {
    match format {
        output::Format::Json => {
            let payload = json!({
                "code": error.code(),
                "message": error.to_string(),
            });
            let mut stderr = std::io::stderr().lock();
            if output::print_json(&mut stderr, &payload).is_err() {
                eprintln!("gwiki: {error}");
            }
        }
        output::Format::Text => eprintln!("gwiki: {error}"),
    }
}

fn command_from_cli(command: CliCommand, scope: ScopeSelection) -> Result<Command, WikiError> {
    match command {
        CliCommand::Init => Ok(Command::Init { scope }),
        CliCommand::Setup(args) => Ok(Command::Setup {
            scope,
            options: args.into(),
        }),
        CliCommand::Index => Ok(Command::Index { scope }),
        CliCommand::Collect => Ok(Command::Collect { scope }),
        CliCommand::IngestFile {
            path,
            no_ai,
            translate,
            target_lang,
            video_frame_interval_seconds,
            transcription_routing,
            vision_routing,
            text_routing,
        } => Ok(Command::IngestFile {
            path,
            scope,
            options: IngestFileOptions {
                no_ai,
                translate,
                target_lang,
                video_frame_interval_seconds,
                transcription_routing,
                vision_routing,
                text_routing,
            },
        }),
        CliCommand::Search(args) => Ok(Command::Search {
            query: args.query,
            scope,
            limit: args.limit,
            include_semantic: !args.no_semantic,
        }),
        CliCommand::Read(args) => {
            let target = match (args.path, args.title) {
                (Some(path), None) => ReadTarget::Path(path),
                (None, Some(title)) => ReadTarget::Title(title),
                _ => {
                    return Err(WikiError::InvalidInput {
                        field: "read",
                        message: "pass exactly one of --path or --title".to_string(),
                    });
                }
            };
            Ok(Command::Read { target, scope })
        }
        CliCommand::Backlinks(args) => Ok(Command::Backlinks {
            page: args.page,
            scope,
        }),
        CliCommand::LinkSuggest(args) => Ok(Command::LinkSuggest {
            scope,
            limit: args.limit,
        }),
        CliCommand::Research(args) => {
            let question = match (args.resume, args.question) {
                (true, question) => question.unwrap_or_default(),
                (false, Some(question)) => question,
                (false, None) => {
                    return Err(WikiError::InvalidInput {
                        field: "research",
                        message: "QUESTION is required unless --resume is set".to_string(),
                    });
                }
            };
            let research_scope = gobby_wiki::resolve_research_scope(&scope)?;
            Ok(Command::Research(gobby_wiki::research::ResearchOptions {
                question,
                scope: research_scope,
                source_constraints: args.source_constraints,
                agent_count: args.agent_count,
                dispatch_task_id: args.task_id,
                resume: args.resume,
                accepted_notes: Vec::new(),
            }))
        }
        CliCommand::Compile(args) => Ok(Command::Compile {
            topic: args.topic,
            outline: args.outline,
            target_kind: args.kind.into(),
            target_page: args.target,
            write_intent: args.write_intent,
            scope,
        }),
        CliCommand::Export(args) => Ok(Command::Export {
            scope,
            command: args.into(),
        }),
        CliCommand::Audit => Ok(Command::Audit { scope }),
        CliCommand::Lint => Ok(Command::Lint { scope }),
        CliCommand::Health => Ok(Command::Health { scope }),
        CliCommand::Status => Ok(Command::Status { scope }),
    }
}

impl From<CompileKind> for gobby_wiki::synthesis::ArticleKind {
    fn from(kind: CompileKind) -> Self {
        match kind {
            CompileKind::Source => Self::Source,
            CompileKind::Concept => Self::Concept,
            CompileKind::Topic => Self::Topic,
        }
    }
}

impl From<ExportArgs> for gobby_wiki::exports::ExportCommand {
    fn from(args: ExportArgs) -> Self {
        match args.command {
            ExportSubcommand::WorkflowAssets { output } => {
                Self::WorkflowAssets { filename: output }
            }
            ExportSubcommand::Report { output, source } => Self::ReportFile {
                filename: output,
                source_path: source,
            },
        }
    }
}

impl From<ScopeArgs> for ScopeSelection {
    fn from(scope: ScopeArgs) -> Self {
        if scope.project {
            Self::project()
        } else if let Some(topic) = scope.topic {
            Self::topic(topic)
        } else {
            Self::global()
        }
    }
}

fn exit_code_for_error(error: &WikiError) -> ExitCode {
    match error {
        WikiError::NotImplemented { .. }
        | WikiError::InvalidInput { .. }
        | WikiError::Index { .. }
        | WikiError::Search { .. }
        | WikiError::InvalidScope { .. } => ExitCode::from(2),
        WikiError::Config { .. }
        | WikiError::Io { .. }
        | WikiError::Json { .. }
        | WikiError::Yaml { .. }
        | WikiError::Registry { .. }
        | WikiError::Daemon { .. }
        | WikiError::Setup { .. } => ExitCode::from(1),
    }
}

impl From<SetupArgs> for gobby_wiki::SetupOptions {
    fn from(args: SetupArgs) -> Self {
        Self {
            standalone: args.standalone,
            database_url: args.database_url,
            no_services: args.no_services,
            falkordb_host: args.falkordb_host,
            falkordb_port: args.falkordb_port,
            falkordb_password: args.falkordb_password,
            qdrant_url: args.qdrant_url,
            embedding_provider: args.embedding_provider,
            embedding_api_base: args.embedding_api_base,
            embedding_model: args.embedding_model,
            embedding_query_prefix: args.embedding_query_prefix,
            embedding_vector_dim: args.embedding_vector_dim,
            embedding_api_key: args.embedding_api_key,
        }
    }
}

#[cfg(test)]
mod tests {
    use gobby_core::ai_context::AiContext;
    use gobby_core::config::{AiRouting, EnvOnlySource};

    use super::*;

    #[test]
    fn ingest_file_cli_flags_map_to_command_options() {
        let command = command_from_cli(
            CliCommand::IngestFile {
                path: PathBuf::from("media/interview.mp3"),
                no_ai: false,
                translate: true,
                target_lang: Some("es".to_string()),
                video_frame_interval_seconds: Some(0),
                transcription_routing: Some(AiRouting::Direct),
                vision_routing: Some(AiRouting::Off),
                text_routing: Some(AiRouting::Daemon),
            },
            ScopeSelection::global(),
        )
        .expect("map ingest-file command");

        let Command::IngestFile { options, .. } = command else {
            panic!("expected ingest-file command");
        };
        assert!(options.translate);
        assert_eq!(options.target_lang.as_deref(), Some("es"));
        assert_eq!(options.video_frame_interval_seconds, Some(0));

        let mut source = EnvOnlySource;
        let mut context = AiContext::resolve(None, &mut source);
        let original_transcribe_route = context.bindings.audio_transcribe.routing;
        options.apply_to_ai_context(&mut context);
        assert_eq!(
            context.bindings.audio_transcribe.routing,
            original_transcribe_route
        );
        assert_eq!(context.bindings.audio_translate.routing, AiRouting::Direct);
        assert_eq!(context.bindings.vision_extract.routing, AiRouting::Off);
        assert_eq!(context.bindings.text_generate.routing, AiRouting::Daemon);
        assert_eq!(
            context.bindings.audio_translate.target_lang.as_deref(),
            Some("es")
        );
    }

    #[test]
    fn setup_cli_flags_map_to_command_options() {
        let command = command_from_cli(
            CliCommand::Setup(SetupArgs {
                standalone: true,
                database_url: Some("postgresql://localhost/gwiki".to_string()),
                no_services: true,
                falkordb_host: Some("127.0.0.2".to_string()),
                falkordb_port: Some(26379),
                falkordb_password: Some("secret".to_string()),
                qdrant_url: Some("http://localhost:7333".to_string()),
                embedding_provider: Some("openai-compatible".to_string()),
                embedding_api_base: Some("http://localhost:1234/v1".to_string()),
                embedding_model: Some("embed-small".to_string()),
                embedding_query_prefix: Some("query: ".to_string()),
                embedding_vector_dim: Some(1024),
                embedding_api_key: Some("api-key".to_string()),
            }),
            ScopeSelection::global(),
        )
        .expect("map setup command");

        let Command::Setup { options, .. } = command else {
            panic!("expected setup command");
        };
        assert!(options.standalone);
        assert_eq!(
            options.database_url.as_deref(),
            Some("postgresql://localhost/gwiki")
        );
        assert!(options.no_services);
        assert_eq!(options.falkordb_host.as_deref(), Some("127.0.0.2"));
        assert_eq!(options.falkordb_port, Some(26379));
        assert_eq!(options.qdrant_url.as_deref(), Some("http://localhost:7333"));
        assert_eq!(options.embedding_vector_dim, Some(1024));
    }
}
