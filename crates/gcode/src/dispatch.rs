use clap::Parser as _;
use gobby_code::{commands, config, freshness, output, setup};

use crate::cli::{self, Cli, Command, EmbeddingsCommand, GraphCommand, VectorCommand};

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
            request.embedding_api_key = embedding_api_key.clone();
            request.falkordb_host = falkordb_host.clone();
            request.falkordb_port = *falkordb_port;
            request.falkordb_password = falkordb_password.clone();
            request.qdrant_url = qdrant_url.clone();
            setup_runner(request, format, cli.quiet)?;
            Ok(true)
        }
        Command::Projects => {
            commands::status::projects(format)?;
            Ok(true)
        }
        Command::Prune { force } => {
            commands::status::prune(*force)?;
            Ok(true)
        }
        Command::Graph {
            command:
                GraphCommand::Clear {
                    project_id: Some(project_id),
                },
        } => {
            let ctx = config::Context::resolve_for_project_id(project_id, cli.quiet)?;
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
    let format = cli::effective_format(cli.format, &cli.command);
    cli::reject_unsupported_grep_flags(&cli.command)?;

    // Commands that must run before Context::resolve() (work on uninitialized projects)
    if dispatch_early_command(&cli, format, commands::setup::run)? {
        return Ok(());
    }

    let ctx = config::Context::resolve(cli.project.as_deref(), cli.quiet)?;

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
            command: GraphCommand::Report { top_n },
        } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::graph::report(&ctx, top_n, format)
        }
        Command::Vector {
            command: VectorCommand::SyncFile { file },
        } => {
            ensure_file_fresh(&ctx, cli.no_freshness, &file)?;
            commands::vector::sync_file(&ctx, &file, format)
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
            before_context,
            after_context,
            context,
            glob,
            max_count,
            line_number: _,
            unsupported_limit: _,
            unsupported_files_with_matches: _,
            unsupported_files_without_match: _,
            unsupported_count: _,
            unsupported_only_matching: _,
            unsupported_invert_match: _,
            unsupported_word_regexp: _,
            unsupported_regexp: _,
            unsupported_recursive: _,
            unsupported_type: _,
            unsupported_type_not: _,
            unsupported_pcre2: _,
            unsupported_multiline: _,
            unsupported_json: _,
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
        Command::Codewiki { out, scope } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::codewiki::run(&ctx, out, scope, format)
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
        } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::graph::usages(&ctx, &symbol_name, limit, offset, format)
        }
        Command::Imports { file } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::graph::imports(&ctx, &file, format)
        }
        Command::BlastRadius { target, depth } => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::graph::blast_radius(&ctx, &target, depth, format)
        }

        Command::RepoOutline => {
            ensure_project_fresh(&ctx, cli.no_freshness)?;
            commands::status::repo_outline(&ctx, format)
        }
    }
}

#[cfg(test)]
mod tests;
