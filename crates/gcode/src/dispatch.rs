use crate::{commands, config, freshness, output};
use clap::Parser as _;

use crate::cli::{self, Cli, Command, EmbeddingsCommand, GraphCommand, VectorCommand};

mod usage;

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
    if let Some(line) = freshness_warning(ctx.quiet, &status) {
        eprintln!("{line}");
    }
}

fn freshness_warning(quiet: bool, status: &freshness::FreshnessStatus) -> Option<String> {
    if quiet {
        return None;
    }
    match status {
        freshness::FreshnessStatus::SkippedBusy => {
            Some("warning: gcode index refresh already running; reading existing index".to_string())
        }
        freshness::FreshnessStatus::Degraded(error) => Some(format!(
            "warning: index refresh failed ({error}); serving existing index \
             (pass --allow-stale to skip this check)"
        )),
        freshness::FreshnessStatus::Checked => None,
    }
}

fn service_config_selection(command: &Command) -> config::ServiceConfigSelection {
    use config::ServiceConfigSelection;

    match command {
        Command::Index { .. } => ServiceConfigSelection::all(),
        Command::Status => ServiceConfigSelection::projection_cleanup(),
        Command::Invalidate { .. } => ServiceConfigSelection::projection_cleanup(),
        Command::Graph { .. }
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
        | Command::Projects
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

fn dispatch_early_command(cli: &Cli, format: output::Format) -> anyhow::Result<bool> {
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
                output::Format::Json => output::print_json(&crate::contract::contract())?,
                output::Format::Text => output::print_text("gcode CLI contract v1")?,
            }
            Ok(true)
        }
        Command::Projects => {
            commands::status::projects(format)?;
            Ok(true)
        }
        Command::Prune {
            force,
            retention_days,
        } => {
            commands::status::prune(*force, cli.project.as_deref(), cli.quiet, *retention_days)?;
            Ok(true)
        }
        Command::Invalidate {
            project_id: Some(project_id),
            force,
        } => {
            let ctx = config::Context::resolve_for_project_id_with_services(
                project_id,
                cli.quiet,
                config::ServiceConfigSelection::projection_cleanup(),
            )?;
            commands::status::invalidate(&ctx, *force, format)?;
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
        Command::Vector {
            command:
                VectorCommand::Clear {
                    project_id: Some(project_id),
                },
        } => {
            let ctx = config::Context::resolve_for_project_id_with_services(
                project_id,
                cli.quiet,
                config::ServiceConfigSelection::projection_cleanup(),
            )?;
            commands::vector::clear(&ctx, format)?;
            Ok(true)
        }
        _ => Ok(false),
    }
}

fn print_typed_error(
    print: impl FnOnce() -> anyhow::Result<()>,
    exit: u8,
) -> std::process::ExitCode {
    if let Err(print_error) = print() {
        eprintln!("Error: {print_error:?}");
        return std::process::ExitCode::FAILURE;
    }
    std::process::ExitCode::from(exit)
}

pub(crate) struct ClassifiedRunError<'a> {
    pub exit: u8,
    printer: Option<Box<dyn FnOnce() -> anyhow::Result<()> + 'a>>,
}

pub(crate) fn classify_run_error(error: &anyhow::Error) -> ClassifiedRunError<'_> {
    if let Some(contract_error) = error.downcast_ref::<commands::graph::GraphSyncContractError>() {
        return ClassifiedRunError {
            exit: contract_error.exit_code(),
            printer: Some(Box::new(|| contract_error.print())),
        };
    }
    if let Some(doctor_exit) =
        error.downcast_ref::<commands::embeddings_doctor::EmbeddingsDoctorExit>()
    {
        return ClassifiedRunError {
            exit: doctor_exit.exit_code(),
            printer: Some(Box::new(|| doctor_exit.print())),
        };
    }
    if let Some(cli_error) = error.downcast_ref::<crate::cli_error::CliError>() {
        return ClassifiedRunError {
            exit: cli_error.exit_status,
            printer: Some(Box::new(|| cli_error.print())),
        };
    }
    if let Some(grant_error) = error.downcast_ref::<gobby_core::grant::GrantError>() {
        let cli_error = crate::cli_error::CliError::grant(grant_error.clone());
        return ClassifiedRunError {
            exit: cli_error.exit_status,
            printer: Some(Box::new(move || cli_error.print())),
        };
    }
    ClassifiedRunError {
        exit: 1,
        printer: None,
    }
}

pub(crate) fn run_with_exit_code() -> std::process::ExitCode {
    match run() {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(error) => {
            let classified = classify_run_error(&error);
            if let Some(print) = classified.printer {
                return print_typed_error(print, classified.exit);
            }
            eprintln!("Error: {error:?}");
            std::process::ExitCode::from(classified.exit)
        }
    }
}

fn run() -> anyhow::Result<()> {
    let cli = match Cli::try_parse() {
        Ok(cli) => cli,
        Err(error) => return usage::handle_parse_error(error),
    };
    init_logger(cli.quiet);
    let format = cli::effective_format(cli.format, &cli.command);

    // Commands that must run before Context::resolve() (work on uninitialized projects)
    if dispatch_early_command(&cli, format)? {
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
        Command::Contract | Command::Init | Command::Projects | Command::Prune { .. } => Ok(()),
        Command::Index {
            path,
            files,
            full,
            require_cpp_semantics,
            sync_projections,
            skip_if_locked,
        } => commands::index::run(
            &ctx,
            path,
            files,
            full,
            require_cpp_semantics,
            sync_projections,
            skip_if_locked,
            format,
        ),
        Command::Status => {
            ensure_project_fresh(&ctx, cli.allow_stale)?;
            commands::status::run(&ctx, format)
        }
        Command::Invalidate {
            project_id: None,
            force,
        } => commands::status::invalidate(&ctx, force, format),
        Command::Invalidate {
            project_id: Some(_),
            ..
        } => Ok(()),
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
            ensure_project_fresh(&ctx, cli.allow_stale)?;
            commands::graph::rebuild(&ctx, format)
        }
        Command::Graph {
            command: GraphCommand::CleanupOrphans,
        } => commands::graph::cleanup_orphans(&ctx, format),
        Command::Graph {
            command: GraphCommand::Report { top_n },
        } => {
            ensure_project_fresh(&ctx, cli.allow_stale)?;
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
                ensure_file_fresh(&ctx, cli.allow_stale, &file)?;
            }
            commands::vector::sync_file(&ctx, &file, allow_missing_indexed_file, format)
        }
        Command::Vector {
            command: VectorCommand::Clear { project_id: None },
        } => commands::vector::clear(&ctx, format),
        Command::Vector {
            command: VectorCommand::Clear {
                project_id: Some(_),
            },
        } => Ok(()),
        Command::Vector {
            command: VectorCommand::Rebuild,
        } => {
            ensure_project_fresh(&ctx, cli.allow_stale)?;
            commands::vector::rebuild(&ctx, format)
        }
        Command::Vector {
            command: VectorCommand::CleanupOrphans,
        } => {
            ensure_project_fresh(&ctx, cli.allow_stale)?;
            commands::vector::cleanup_orphans(&ctx, format)
        }
        Command::Embeddings {
            command: EmbeddingsCommand::Doctor,
        } => commands::embeddings_doctor::run(&ctx),
        Command::Graph {
            command: GraphCommand::Overview { limit },
        } => {
            ensure_project_fresh(&ctx, cli.allow_stale)?;
            commands::graph::overview(&ctx, limit, format)
        }
        Command::Graph {
            command: GraphCommand::File { file },
        } => {
            ensure_file_fresh(&ctx, cli.allow_stale, &file)?;
            commands::graph::file(&ctx, &file, format)
        }
        Command::Graph {
            command: GraphCommand::Neighbors { symbol_id, limit },
        } => {
            ensure_symbol_fresh(&ctx, cli.allow_stale, &symbol_id)?;
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
            ensure_project_fresh(&ctx, cli.allow_stale)?;
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
            ensure_project_fresh(&ctx, cli.allow_stale)?;
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
            ensure_project_fresh(&ctx, cli.allow_stale)?;
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
            ensure_project_fresh(&ctx, cli.allow_stale)?;
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
            ensure_project_fresh(&ctx, cli.allow_stale)?;
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
            files_with_matches,
            extended_regexp: _,
            line_number: _,
            recursive: _,
            recursive_dereference: _,
            before_context,
            after_context,
            context,
            glob,
            max_count,
        } => {
            ensure_project_fresh(&ctx, cli.allow_stale)?;
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
                    files_with_matches,
                    format,
                },
            )
        }

        Command::Outline { file } => {
            ensure_file_fresh(&ctx, cli.allow_stale, &file)?;
            commands::symbols::outline(&ctx, &file, format, cli.verbose)
        }
        Command::Symbol { id } => {
            ensure_symbol_fresh(&ctx, cli.allow_stale, &id)?;
            commands::symbols::symbol(&ctx, &id, format)
        }
        Command::SymbolAt { location, line } => {
            let file = commands::symbol_at::requested_file_for_freshness(&ctx, &location, line)?;
            ensure_file_fresh(&ctx, cli.allow_stale, &file)?;
            commands::symbol_at::run(&ctx, &location, line, format)
        }
        Command::Symbols { ids } => {
            ensure_project_fresh(&ctx, cli.allow_stale)?;
            commands::symbols::symbols(&ctx, &ids, format)
        }
        Command::Kinds => {
            ensure_project_fresh(&ctx, cli.allow_stale)?;
            commands::symbols::kinds(&ctx, format)
        }
        Command::Tree => {
            ensure_project_fresh(&ctx, cli.allow_stale)?;
            commands::symbols::tree(&ctx, format)
        }
        Command::Callers {
            symbol_name,
            limit,
            offset,
        } => {
            ensure_project_fresh(&ctx, cli.allow_stale)?;
            commands::graph::callers(&ctx, &symbol_name, limit, offset, format)
        }
        Command::Usages {
            symbol_name,
            limit,
            offset,
            token_budget,
        } => {
            ensure_project_fresh(&ctx, cli.allow_stale)?;
            commands::graph::usages(&ctx, &symbol_name, limit, offset, token_budget, format)
        }
        Command::Imports { file } => {
            ensure_project_fresh(&ctx, cli.allow_stale)?;
            commands::graph::imports(&ctx, &file, format)
        }
        Command::Path {
            symbol_a,
            symbol_b,
            max_depth,
        } => {
            ensure_project_fresh(&ctx, cli.allow_stale)?;
            commands::graph::path(&ctx, &symbol_a, &symbol_b, max_depth, format)
        }
        Command::BlastRadius {
            target,
            depth,
            token_budget,
        } => {
            ensure_project_fresh(&ctx, cli.allow_stale)?;
            commands::graph::blast_radius(&ctx, &target, depth, token_budget, format)
        }

        Command::RepoOutline => {
            ensure_project_fresh(&ctx, cli.allow_stale)?;
            commands::status::repo_outline(&ctx, format)
        }
    }
}

#[cfg(test)]
mod tests;
