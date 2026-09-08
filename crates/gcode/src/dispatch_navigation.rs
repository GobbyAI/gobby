//! Dispatch for read-only code navigation commands.

use std::path::Path;

use crate::cli::Command;
use crate::{commands, config, output};

pub(super) fn dispatch(
    ctx: &config::Context,
    cwd: &Path,
    command: &Command,
    format: output::Format,
    token_budget: Option<usize>,
    allow_stale: bool,
    verbose: bool,
) -> anyhow::Result<bool> {
    match command {
        Command::Search {
            query,
            paths,
            limit,
            offset,
            kind,
            language,
            token_budget: _,
        } => {
            let paths = resolve_filters(ctx, cwd, paths)?;
            super::ensure_project_fresh(ctx, allow_stale)?;
            commands::search::search(
                ctx,
                query,
                commands::search::SearchOptions {
                    limit: *limit,
                    offset: *offset,
                    kind: kind.as_deref(),
                    language: language.as_deref(),
                    paths: &paths,
                    format,
                    with_graph: true,
                    token_budget,
                    verbose,
                },
            )?;
        }
        Command::SearchSymbol {
            query,
            paths,
            limit,
            offset,
            kind,
            language,
            with_graph,
            token_budget: _,
        } => {
            let paths = resolve_filters(ctx, cwd, paths)?;
            super::ensure_project_fresh(ctx, allow_stale)?;
            commands::search::search_symbol(
                ctx,
                query,
                commands::search::SearchOptions {
                    limit: *limit,
                    offset: *offset,
                    kind: kind.as_deref(),
                    language: language.as_deref(),
                    paths: &paths,
                    format,
                    with_graph: *with_graph,
                    token_budget,
                    verbose,
                },
            )?;
        }
        Command::SearchText {
            query,
            paths,
            limit,
            offset,
            language,
            token_budget: _,
        } => {
            let paths = resolve_filters(ctx, cwd, paths)?;
            super::ensure_project_fresh(ctx, allow_stale)?;
            commands::search::search_text(
                ctx,
                query,
                commands::search::TextSearchOptions {
                    limit: *limit,
                    offset: *offset,
                    language: language.as_deref(),
                    paths: &paths,
                    format,
                    token_budget,
                    verbose,
                },
            )?;
        }
        Command::SearchContent {
            query,
            paths,
            limit,
            offset,
            language,
            token_budget: _,
        } => {
            let paths = resolve_filters(ctx, cwd, paths)?;
            super::ensure_project_fresh(ctx, allow_stale)?;
            commands::search::search_content(
                ctx,
                query,
                commands::search::TextSearchOptions {
                    limit: *limit,
                    offset: *offset,
                    language: language.as_deref(),
                    paths: &paths,
                    format,
                    token_budget,
                    verbose,
                },
            )?;
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
            offset,
            token_budget: _,
        } => {
            let paths = resolve_filters(ctx, cwd, paths)?;
            let globs = resolve_globs(ctx, cwd, glob)?;
            super::ensure_project_fresh(ctx, allow_stale)?;
            commands::grep::run(
                ctx,
                commands::grep::GrepOptions {
                    pattern,
                    paths: &paths,
                    globs: &globs,
                    fixed_strings: *fixed_strings,
                    ignore_case: *ignore_case,
                    word: *word,
                    context: *context,
                    before_context: *before_context,
                    after_context: *after_context,
                    max_count: *max_count,
                    offset: *offset,
                    token_budget,
                    files_with_matches: *files_with_matches,
                    format,
                },
            )?;
        }
        Command::Outline {
            file,
            limit,
            offset,
            token_budget: _,
        } => {
            let file = super::resolve_exact_file(ctx, cwd, file)?;
            super::ensure_file_fresh(ctx, allow_stale, &file)?;
            commands::symbols::outline(ctx, &file, *limit, *offset, token_budget, format, verbose)?;
        }
        Command::Symbol { id } => {
            super::ensure_symbol_fresh(ctx, allow_stale, id)?;
            commands::symbols::symbol(ctx, id, format)?;
        }
        Command::SymbolAt { location, line } => {
            let file =
                commands::symbol_at::requested_file_for_freshness(ctx, cwd, location, *line)?;
            super::ensure_file_fresh(ctx, allow_stale, &file)?;
            commands::symbol_at::run(ctx, cwd, location, *line, format)?;
        }
        Command::Symbols {
            ids,
            limit,
            offset,
            token_budget: _,
        } => {
            super::ensure_project_fresh(ctx, allow_stale)?;
            commands::symbols::symbols(ctx, ids, *limit, *offset, token_budget, format)?;
        }
        Command::Kinds {
            limit,
            offset,
            token_budget: _,
        } => {
            super::ensure_project_fresh(ctx, allow_stale)?;
            commands::symbols::kinds(ctx, *limit, *offset, token_budget, format)?;
        }
        Command::Tree {
            paths,
            limit,
            offset,
            token_budget: _,
        } => {
            let paths = resolve_filters(ctx, cwd, paths)?;
            super::ensure_project_fresh(ctx, allow_stale)?;
            commands::symbols::tree(ctx, &paths, *limit, *offset, token_budget, format)?;
        }
        Command::Callers {
            symbol_name,
            limit,
            offset,
            token_budget: _,
        } => {
            super::ensure_project_fresh(ctx, allow_stale)?;
            commands::graph::callers(ctx, symbol_name, *limit, *offset, token_budget, format)?;
        }
        Command::Callees {
            symbol_name,
            limit,
            offset,
            token_budget: _,
        } => {
            super::ensure_project_fresh(ctx, allow_stale)?;
            commands::graph::callees(ctx, symbol_name, *limit, *offset, token_budget, format)?;
        }
        Command::Usages {
            symbol_name,
            limit,
            offset,
            token_budget: _,
        } => {
            super::ensure_project_fresh(ctx, allow_stale)?;
            commands::graph::usages(ctx, symbol_name, *limit, *offset, token_budget, format)?;
        }
        Command::Imports {
            file,
            limit,
            offset,
            token_budget: _,
        } => {
            let file = super::resolve_exact_file(ctx, cwd, file)?;
            super::ensure_project_fresh(ctx, allow_stale)?;
            commands::graph::imports(ctx, &file, *limit, *offset, token_budget, format)?;
        }
        Command::Path {
            symbol_a,
            symbol_b,
            max_depth,
        } => {
            super::ensure_project_fresh(ctx, allow_stale)?;
            commands::graph::path(ctx, symbol_a, symbol_b, *max_depth, format)?;
        }
        Command::BlastRadius {
            target,
            depth,
            limit,
            offset,
            token_budget: _,
        } => {
            super::ensure_project_fresh(ctx, allow_stale)?;
            commands::graph::blast_radius(
                ctx,
                target,
                *depth,
                *limit,
                *offset,
                token_budget,
                format,
            )?;
        }
        Command::RepoOutline {
            limit,
            offset,
            token_budget: _,
        } => {
            super::ensure_project_fresh(ctx, allow_stale)?;
            commands::status::repo_outline(ctx, *limit, *offset, token_budget, format)?;
        }
        _ => return Ok(false),
    }
    Ok(true)
}

fn resolve_filters(
    ctx: &config::Context,
    cwd: &Path,
    paths: &[String],
) -> anyhow::Result<Vec<String>> {
    paths
        .iter()
        .map(|path| {
            commands::scope::resolve_path_input(
                ctx,
                cwd,
                commands::scope::ScopedPathInput::Filter(path),
            )
            .map_err(Into::into)
        })
        .collect()
}

fn resolve_globs(
    ctx: &config::Context,
    cwd: &Path,
    globs: &[String],
) -> anyhow::Result<Vec<String>> {
    globs
        .iter()
        .map(|glob| {
            commands::scope::resolve_path_input(
                ctx,
                cwd,
                commands::scope::ScopedPathInput::Glob(glob),
            )
            .map_err(Into::into)
        })
        .collect()
}
