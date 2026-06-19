use clap::Parser as _;
use gobby_code::{commands, config, freshness, output, setup};

use crate::cli::{self, AiRouteArg, Cli, Command, EmbeddingsCommand, GraphCommand, VectorCommand};

static STDERR_LOGGER: StderrLogger = StderrLogger;

struct StderrLogger;

impl log::Log for StderrLogger {
    fn enabled(&self, metadata: &log::Metadata<'_>) -> bool {
        metadata.level() <= log::max_level()
    }

    fn log(&self, record: &log::Record<'_>) {
        if self.enabled(record.metadata()) {
            eprintln!("{}: {}", record.level(), record.args());
        }
    }

    fn flush(&self) {}
}

fn init_logger(quiet: bool) {
    let rust_log = std::env::var("RUST_LOG").ok();
    let _ = log::set_logger(&STDERR_LOGGER);
    log::set_max_level(stderr_log_level(quiet, rust_log.as_deref()));
}

fn stderr_log_level(quiet: bool, rust_log: Option<&str>) -> log::LevelFilter {
    if quiet {
        return log::LevelFilter::Off;
    }
    rust_log
        .and_then(|value| value.trim().parse().ok())
        .unwrap_or(log::LevelFilter::Warn)
}

fn ensure_project_fresh(ctx: &config::Context, disabled: bool) -> anyhow::Result<()> {
    if !disabled {
        warn_if_busy(
            ctx,
            freshness::ensure_fresh(ctx, freshness::FreshnessScope::Project)?,
        );
    }
    Ok(())
}

fn ensure_files_fresh(
    ctx: &config::Context,
    disabled: bool,
    files: Vec<std::path::PathBuf>,
) -> anyhow::Result<()> {
    if !disabled {
        warn_if_busy(
            ctx,
            freshness::ensure_fresh(ctx, freshness::FreshnessScope::Files(files))?,
        );
    }
    Ok(())
}

fn ensure_file_fresh(ctx: &config::Context, disabled: bool, file: &str) -> anyhow::Result<()> {
    ensure_files_fresh(ctx, disabled, vec![std::path::PathBuf::from(file)])
}

fn ensure_symbol_fresh(ctx: &config::Context, disabled: bool, id: &str) -> anyhow::Result<()> {
    if !disabled {
        warn_if_busy(ctx, freshness::ensure_symbol_fresh(ctx, id)?);
    }
    Ok(())
}

fn warn_if_busy(ctx: &config::Context, status: freshness::FreshnessStatus) {
    if matches!(status, freshness::FreshnessStatus::SkippedBusy) && !ctx.quiet {
        eprintln!("warning: gcode index refresh already running; reading existing index");
    }
}

fn service_config_selection(command: &Command) -> config::ServiceConfigSelection {
    use config::ServiceConfigSelection;

    match command {
        Command::Index { .. } => ServiceConfigSelection::all(),
        Command::Status => ServiceConfigSelection::projection_cleanup(),
        Command::Graph { .. }
        | Command::Codewiki { .. }
        | Command::Callers { .. }
        | Command::Usages { .. }
        | Command::Imports { .. }
        | Command::Path { .. }
        | Command::BlastRadius { .. } => ServiceConfigSelection::falkordb_only(),
        Command::Vector {
            command: VectorCommand::CleanupOrphans,
        } => ServiceConfigSelection::qdrant_only(),
        Command::Vector { .. } | Command::Embeddings { .. } => ServiceConfigSelection::vectors(),
        Command::Search { .. } => ServiceConfigSelection::hybrid_search(),
        Command::SearchSymbol { with_graph, .. } => {
            if *with_graph {
                ServiceConfigSelection::falkordb_only()
            } else {
                ServiceConfigSelection::database_only()
            }
        }
        Command::Contract
        | Command::Init
        | Command::Setup { .. }
        | Command::Projects
        | Command::Invalidate { .. }
        | Command::SearchText { .. }
        | Command::SearchContent { .. }
        | Command::Grep { .. }
        | Command::Outline { .. }
        | Command::Symbol { .. }
        | Command::SymbolAt { .. }
        | Command::Symbols { .. }
        | Command::Kinds
        | Command::Tree
        | Command::RepoOutline => ServiceConfigSelection::database_only(),
        Command::Prune { .. } => ServiceConfigSelection::projection_cleanup(),
    }
}

fn codewiki_ai_options(
    ai: Option<AiRouteArg>,
    ai_depth: cli::AiDepthArg,
    ai_prose_depth: cli::AiProseDepthArg,
    ai_register: Option<cli::AiRegisterArg>,
    ai_aggregate_profile: Option<String>,
    ai_verify_profile: Option<String>,
) -> commands::codewiki::CodewikiAiOptions {
    commands::codewiki::CodewikiAiOptions {
        routing: ai.map(AiRouteArg::into),
        depth: ai_depth.into(),
        prose_depth: ai_prose_depth.into(),
        register: ai_register.map(Into::into),
        aggregate_profile: ai_aggregate_profile,
        verify_profile: ai_verify_profile,
        verify_model: None,
        verify_api_key: None,
    }
}

fn dispatch_early_command<F>(
    cli: &Cli,
    format: output::Format,
    setup_runner: F,
) -> anyhow::Result<bool>
where
    F: FnOnce(setup::StandaloneSetupRequest, output::Format, bool) -> anyhow::Result<()>,
{
    match &cli.command {
        Command::Init => {
            let root = match &cli.project {
                Some(p) => std::path::PathBuf::from(p).canonicalize()?,
                None => config::detect_project_root()?,
            };
            commands::init::run(&root, format, cli.quiet)?;
            Ok(true)
        }
        Command::Contract => {
            match format {
                output::Format::Json => output::print_json(&gobby_code::contract::contract())?,
                output::Format::Text => output::print_text("gcode CLI contract v1")?,
            }
            Ok(true)
        }
        Command::Setup {
            standalone,
            database_url,
            no_services,
            overwrite_code_index,
            schema,
            embedding_provider,
            embedding_api_base,
            embedding_model,
            embedding_query_prefix,
            embedding_vector_dim,
            embedding_api_key,
            falkordb_host,
            falkordb_port,
            falkordb_password,
            qdrant_url,
        } => {
            let mut request = setup::StandaloneSetupRequest::new(
                *standalone,
                database_url.clone(),
                Some(schema.clone()),
            );
            request.no_services = *no_services;
            request.overwrite_code_index = *overwrite_code_index;
            request.embedding_provider = embedding_provider.clone();
            request.embedding_api_base = embedding_api_base.clone();
            request.embedding_model = embedding_model.clone();
            request.embedding_query_prefix = embedding_query_prefix.clone();
            request.embedding_vector_dim = *embedding_vector_dim;
            request.embedding_api_key = embedding_api_key.clone().into();
            request.falkordb_host = falkordb_host.clone();
            request.falkordb_port = *falkordb_port;
            request.falkordb_password = falkordb_password.clone().into();
            request.qdrant_url = qdrant_url.clone();
            setup_runner(request, format, cli.quiet)?;
            Ok(true)
        }
        Command::Projects => {
            commands::status::projects(format)?;
            Ok(true)
        }
        Command::Prune { force } => {
            commands::status::prune(*force, cli.project.as_deref(), cli.quiet)?;
            Ok(true)
        }
        Command::Graph {
            command:
                GraphCommand::Clear {
                    project_id: Some(project_id),
                },
        } => {
            let ctx = config::Context::resolve_for_project_id_with_services(
                project_id,
                cli.quiet,
                config::ServiceConfigSelection::falkordb_only(),
            )?;
            commands::graph::clear(&ctx, format)?;
            Ok(true)
        }
        _ => Ok(false),
    }
}

pub(crate) fn run_with_exit_code() -> std::process::ExitCode {
    match run() {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(error) => {
            if let Some(contract_error) =
                error.downcast_ref::<commands::graph::GraphSyncContractError>()
            {
                if let Err(print_error) = contract_error.print() {
                    eprintln!("Error: {print_error:?}");
                    return std::process::ExitCode::FAILURE;
                }
                return std::process::ExitCode::from(contract_error.exit_code());
            }
            if let Some(doctor_exit) =
                error.downcast_ref::<commands::embeddings_doctor::EmbeddingsDoctorExit>()
            {
                if let Err(print_error) = doctor_exit.print() {
                    eprintln!("Error: {print_error:?}");
                    return std::process::ExitCode::FAILURE;
                }
                return std::process::ExitCode::from(doctor_exit.exit_code());
            }
            eprintln!("Error: {error:?}");
            std::process::ExitCode::FAILURE
        }
    }
}

fn run() -> anyhow::Result<()> {
    let cli = Cli::parse();
    init_logger(cli.quiet);
    let format = cli::effective_format(cli.format, &cli.command);

    // Commands that must run before Context::resolve() (work on uninitialized projects)
    if dispatch_early_command(&cli, format, commands::setup::run)? {
        return Ok(());
    }

    let ctx = config::Context::resolve_with_services(
        cli.project.as_deref(),
        cli.quiet,
        service_config_selection(&cli.command),
    )?;

    match cli.command {
        // These commands are handled before Context::resolve(); this arm keeps the
        // exhaustive match explicit if the early-dispatch block returns normally.
        Command::Contract
        | Command::Init
        | Command::Setup { .. }
        | Command::Projects
        | Command::Prune { .. } => Ok(()),
        Command::Index {
            path,
            files,
            full,
            require_cpp_semantics,
            sync_projections,
        } => commands::index::run(
            &ctx,
            path,
            files,
            full,
            require_cpp_semantics,
            sync_projections,
            format,
        ),
        Command::Status => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::status::run(&ctx, format)
        }
        Command::Invalidate { force } => commands::status::invalidate(&ctx, force),
        Command::Graph {
            command:
                GraphCommand::SyncFile {
                    file,
                    allow_missing_indexed_file,
                },
        } => commands::graph::sync_file(&ctx, &file, allow_missing_indexed_file, format),
        Command::Graph {
            command: GraphCommand::Clear { project_id: None },
        } => commands::graph::clear(&ctx, format),
        Command::Graph {
            command: GraphCommand::Clear {
                project_id: Some(_),
            },
        } => Ok(()),
        Command::Graph {
            command: GraphCommand::Rebuild,
        } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::graph::rebuild(&ctx, format)
        }
        Command::Graph {
            command: GraphCommand::CleanupOrphans,
        } => commands::graph::cleanup_orphans(&ctx, format),
        Command::Graph {
            command: GraphCommand::Report { top_n },
        } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::graph::report(&ctx, top_n, format)
        }
        Command::Vector {
            command:
                VectorCommand::SyncFile {
                    file,
                    allow_missing_indexed_file,
                },
        } => {
            if !allow_missing_indexed_file {
                ensure_file_fresh(&ctx, cli.no_freshness, &file)?;
            }
            commands::vector::sync_file(&ctx, &file, allow_missing_indexed_file, format)
        }
        Command::Vector {
            command: VectorCommand::Clear,
        } => commands::vector::clear(&ctx, format),
        Command::Vector {
            command: VectorCommand::Rebuild,
        } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::vector::rebuild(&ctx, format)
        }
        Command::Vector {
            command: VectorCommand::CleanupOrphans,
        } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::vector::cleanup_orphans(&ctx, format)
        }
        Command::Embeddings {
            command: EmbeddingsCommand::Doctor,
        } => commands::embeddings_doctor::run(&ctx),
        Command::Graph {
            command: GraphCommand::Overview { limit },
        } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::graph::overview(&ctx, limit, format)
        }
        Command::Graph {
            command: GraphCommand::File { file },
        } => {
            ensure_file_fresh(&ctx, cli.no_freshness, &file)?;
            commands::graph::file(&ctx, &file, format)
        }
        Command::Graph {
            command: GraphCommand::Neighbors { symbol_id, limit },
        } => {
            ensure_symbol_fresh(&ctx, cli.no_freshness, &symbol_id)?;
            commands::graph::neighbors(&ctx, &symbol_id, limit, format)
        }
        Command::Graph {
            command:
                GraphCommand::BlastRadius {
                    symbol_id,
                    file,
                    depth,
                    limit,
                },
        } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::graph::graph_blast_radius(
                &ctx,
                symbol_id.as_deref(),
                file.as_deref(),
                depth,
                limit,
                format,
            )
        }

        Command::Search {
            query,
            paths,
            limit,
            offset,
            kind,
            language,
            token_budget,
        } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::search::search(
                &ctx,
                &query,
                commands::search::SearchOptions {
                    limit,
                    offset,
                    kind: kind.as_deref(),
                    language: language.as_deref(),
                    paths: &paths,
                    format,
                    with_graph: true,
                    token_budget,
                },
            )
        }
        Command::SearchSymbol {
            query,
            paths,
            limit,
            offset,
            kind,
            language,
            with_graph,
        } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::search::search_symbol(
                &ctx,
                &query,
                commands::search::SearchOptions {
                    limit,
                    offset,
                    kind: kind.as_deref(),
                    language: language.as_deref(),
                    paths: &paths,
                    format,
                    with_graph,
                    token_budget: None,
                },
            )
        }
        Command::SearchText {
            query,
            paths,
            limit,
            offset,
            language,
        } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::search::search_text(
                &ctx,
                &query,
                limit,
                offset,
                language.as_deref(),
                &paths,
                format,
            )
        }
        Command::SearchContent {
            query,
            paths,
            limit,
            offset,
            language,
        } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::search::search_content(
                &ctx,
                &query,
                limit,
                offset,
                language.as_deref(),
                &paths,
                format,
            )
        }
        Command::Grep {
            pattern,
            paths,
            fixed_strings,
            ignore_case,
            word,
            before_context,
            after_context,
            context,
            glob,
            max_count,
        } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::grep::run(
                &ctx,
                commands::grep::GrepOptions {
                    pattern: &pattern,
                    paths: &paths,
                    globs: &glob,
                    fixed_strings,
                    ignore_case,
                    word,
                    context,
                    before_context,
                    after_context,
                    max_count,
                    format,
                },
            )
        }

        Command::Outline { file, summarize } => {
            ensure_file_fresh(&ctx, cli.no_freshness, &file)?;
            commands::symbols::outline(&ctx, &file, format, cli.verbose, summarize)
        }
        Command::Symbol { id } => {
            ensure_symbol_fresh(&ctx, cli.no_freshness, &id)?;
            commands::symbols::symbol(&ctx, &id, format)
        }
        Command::SymbolAt { location, line } => {
            let file = commands::symbol_at::requested_file_for_freshness(&ctx, &location, line)?;
            ensure_file_fresh(&ctx, cli.no_freshness, &file)?;
            commands::symbol_at::run(&ctx, &location, line, format)
        }
        Command::Symbols { ids } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::symbols::symbols(&ctx, &ids, format)
        }
        Command::Kinds => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::symbols::kinds(&ctx, format)
        }
        Command::Tree => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::symbols::tree(&ctx, format)
        }
        Command::Codewiki {
            out,
            scope,
            ai,
            ai_depth,
            ai_aggregate_profile,
            ai_verify_profile,
            ai_prose_depth,
            ai_register,
            edge_limit,
            include_docs,
            repair_citations,
        } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            if repair_citations {
                return commands::codewiki::run_repair(&ctx, out, format);
            }
            commands::codewiki::run(
                &ctx,
                out,
                scope,
                codewiki_ai_options(
                    ai,
                    ai_depth,
                    ai_prose_depth,
                    ai_register,
                    ai_aggregate_profile,
                    ai_verify_profile,
                ),
                edge_limit,
                include_docs,
                format,
                cli.verbose,
            )
        }

        Command::Callers {
            symbol_name,
            limit,
            offset,
        } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::graph::callers(&ctx, &symbol_name, limit, offset, format)
        }
        Command::Usages {
            symbol_name,
            limit,
            offset,
            token_budget,
        } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::graph::usages(&ctx, &symbol_name, limit, offset, token_budget, format)
        }
        Command::Imports { file } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::graph::imports(&ctx, &file, format)
        }
        Command::Path {
            symbol_a,
            symbol_b,
            max_depth,
        } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::graph::path(&ctx, &symbol_a, &symbol_b, max_depth, format)
        }
        Command::BlastRadius {
            target,
            depth,
            token_budget,
        } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::graph::blast_radius(&ctx, &target, depth, token_budget, format)
        }

        Command::RepoOutline => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::status::repo_outline(&ctx, format)
        }
    }
}

#[cfg(test)]
mod tests;
