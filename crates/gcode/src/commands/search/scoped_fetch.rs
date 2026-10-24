//! Ranked text and symbol retrieval through the effective workspace scope.

use super::*;

pub fn search_symbol(ctx: &Context, query: &str, options: SearchOptions<'_>) -> anyhow::Result<()> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let expanded_paths = fts::expand_paths(options.paths);
    let path_patterns = fts::compile_patterns(&expanded_paths)?;
    let fetch_limit = ((options.offset + options.limit) * 3).max(200);
    let exact_results = fts::search_symbols_exact_first_visible(
        &mut conn,
        query,
        ctx,
        options.kind,
        options.language,
        &expanded_paths,
        fetch_limit,
    )?;

    if options.with_graph {
        return search_symbol_with_graph(
            ctx,
            query,
            options,
            exact_results,
            SymbolGraphSearchContext {
                conn: &mut conn,
                path_patterns: &path_patterns,
                expanded_paths: &expanded_paths,
            },
        );
    }

    let all_results: Vec<_> = exact_results
        .into_iter()
        .filter(|s| {
            symbol_matches_local_filters(ctx, s, options.kind, options.language, &path_patterns)
        })
        .collect();
    let all_results = retain_indexed_results(&mut conn, ctx, all_results, |s| s.file_path.as_str());
    let total = all_results.len();
    let results: Vec<_> = all_results
        .into_iter()
        .skip(options.offset)
        .take(options.limit)
        .collect();

    let is_empty = results.is_empty();
    let lane_hint = search_lane_hint(
        query,
        options.paths,
        options.kind,
        options.language,
        is_empty,
        false,
    );
    let path_hint =
        fts::path_filter_requires_post_filter(&expanded_paths).then(path_filter_post_filter_hint);
    let hint = token_budget::combine_hints(lane_hint, path_hint);
    output::print_empty_diagnostic(ctx, is_empty, options.offset, total, hint.as_deref());
    let results = results
        .into_iter()
        .map(|symbol| {
            let mut result = symbol.to_brief();
            result.score = exact_tier_score(query, &symbol);
            result
        })
        .collect();
    let meta = output::SearchPageMeta {
        project_id: &ctx.project_id,
        total,
        offset: options.offset,
        limit: options.limit,
        hint: hint.as_deref(),
        warnings: &[],
    };
    let page = output::paginate(
        results,
        meta,
        options.token_budget,
        options.format,
        |rows| output::render_exact_results(rows, options.verbose),
    );
    if matches!(options.format, Format::Text) {
        output::print_search_hint(ctx, is_empty, hint.as_deref());
    }
    output::print_page(&page, meta, options.format, |rows| {
        output::render_exact_results(rows, options.verbose)
    })
}

struct SymbolGraphSearchContext<'a> {
    conn: &'a mut postgres::Client,
    path_patterns: &'a [glob::Pattern],
    expanded_paths: &'a [String],
}

fn search_symbol_with_graph(
    ctx: &Context,
    query: &str,
    options: SearchOptions<'_>,
    exact_results: Vec<Symbol>,
    graph_context: SymbolGraphSearchContext<'_>,
) -> anyhow::Result<()> {
    let SymbolGraphSearchContext {
        conn,
        path_patterns,
        expanded_paths,
    } = graph_context;
    let exact_ids: Vec<String> = exact_results.iter().map(|s| s.id.clone()).collect();
    let seed_ids: Vec<String> = exact_ids.iter().take(5).cloned().collect();
    let graph_ids = graph_boost::graph_boost(ctx, Some(&mut *conn), query);
    let expand_ids = graph_boost::graph_expand(ctx, Some(&mut *conn), &seed_ids);

    let mut sources: Vec<(&str, Vec<String>)> = Vec::new();
    if !exact_ids.is_empty() {
        sources.push(("exact", exact_ids));
    }
    if !graph_ids.is_empty() {
        sources.push(("graph", graph_ids));
    }
    if !expand_ids.is_empty() {
        sources.push(("graph_expand", expand_ids));
    }

    let merged = rrf::merge(sources);
    let symbol_cache: HashMap<String, Symbol> = exact_results
        .into_iter()
        .map(|sym| (sym.id.clone(), sym))
        .collect();
    let resolved = resolve_graph_symbols(
        conn,
        ctx,
        &merged,
        symbol_cache,
        options.kind,
        options.language,
        path_patterns,
    )?;
    let mut all_resolved = retain_indexed_results(conn, ctx, resolved, |r| r.0.file_path.as_str());

    all_resolved.sort_by(|a, b| {
        exact_tier(query, &a.0)
            .cmp(&exact_tier(query, &b.0))
            .then_with(|| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal))
            .then_with(|| a.0.file_path.cmp(&b.0.file_path))
            .then_with(|| a.0.line_start.cmp(&b.0.line_start))
    });

    let total = all_resolved.len();
    let results: Vec<_> = all_resolved
        .into_iter()
        .skip(options.offset)
        .take(options.limit)
        .map(|(s, rrf_score, sources)| {
            let mut result = s.to_brief();
            result.score = final_rank_score(query, &s, rrf_score);
            result.rrf_score = Some(rrf_score);
            result.sources = Some(sources);
            result
        })
        .collect();

    let is_empty = results.is_empty();
    let lane_hint = search_lane_hint(
        query,
        options.paths,
        options.kind,
        options.language,
        is_empty,
        false,
    );
    let path_hint =
        fts::path_filter_requires_post_filter(expanded_paths).then(path_filter_post_filter_hint);
    let hint = token_budget::combine_hints(lane_hint, path_hint);
    output::print_empty_diagnostic(ctx, is_empty, options.offset, total, hint.as_deref());
    let meta = output::SearchPageMeta {
        project_id: &ctx.project_id,
        total,
        offset: options.offset,
        limit: options.limit,
        hint: hint.as_deref(),
        warnings: &[],
    };
    let page = output::paginate(
        results,
        meta,
        options.token_budget,
        options.format,
        |rows| output::render_exact_results(rows, options.verbose),
    );
    if matches!(options.format, Format::Text) {
        output::print_search_hint(ctx, is_empty, hint.as_deref());
    }
    output::print_page(&page, meta, options.format, |rows| {
        output::render_exact_results(rows, options.verbose)
    })
}

pub fn search_text(
    ctx: &Context,
    query: &str,
    options: TextSearchOptions<'_>,
) -> anyhow::Result<()> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let expanded_paths = fts::expand_paths(options.paths);
    let path_patterns = fts::compile_patterns(&expanded_paths)?;
    let has_path_filters = !expanded_paths.is_empty();
    let fetch_limit = if has_path_filters {
        fts::FILTERED_FETCH_CAP
    } else {
        ((options.offset + options.limit) * 3).max(200)
    };
    let all_results = fts::search_text_visible(
        &mut conn,
        query,
        ctx,
        options.language,
        &expanded_paths,
        fetch_limit,
    )?;
    let cap_hint = (has_path_filters && all_results.len() >= fts::FILTERED_FETCH_CAP)
        .then(filtered_fetch_cap_hint);
    let path_hint =
        fts::path_filter_requires_post_filter(&expanded_paths).then(path_filter_post_filter_hint);
    let hint = token_budget::combine_hints(cap_hint, path_hint);
    let all_results: Vec<_> = all_results
        .into_iter()
        .filter(|r| {
            options.language.is_none_or(|lang| r.language == lang)
                && path_matches_filters(&path_patterns, &r.file_path)
        })
        .collect();
    let all_results = retain_indexed_results(&mut conn, ctx, all_results, |r| r.file_path.as_str());
    let total = if has_path_filters {
        all_results.len()
    } else {
        fts::count_text_visible(&mut conn, query, ctx, options.language, &expanded_paths)?
    };
    let results: Vec<_> = all_results
        .into_iter()
        .skip(options.offset)
        .take(options.limit)
        .collect();

    let is_empty = results.is_empty();
    let lane_hint = search_lane_hint(query, options.paths, None, options.language, is_empty, true);
    let hint = token_budget::combine_hints(lane_hint, hint);
    output::print_empty_diagnostic(ctx, is_empty, options.offset, total, hint.as_deref());
    let meta = output::SearchPageMeta {
        project_id: &ctx.project_id,
        total,
        offset: options.offset,
        limit: options.limit,
        hint: hint.as_deref(),
        warnings: &[],
    };
    let page = output::paginate(
        results,
        meta,
        options.token_budget,
        options.format,
        |rows| output::render_search_results(rows, options.verbose),
    );
    if matches!(options.format, Format::Text) {
        output::print_search_hint(ctx, is_empty, hint.as_deref());
    }
    output::print_page(&page, meta, options.format, |rows| {
        output::render_search_results(rows, options.verbose)
    })
}

pub fn search_content(
    ctx: &Context,
    query: &str,
    options: TextSearchOptions<'_>,
) -> anyhow::Result<()> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let expanded_paths = fts::expand_paths(options.paths);
    let path_patterns = fts::compile_patterns(&expanded_paths)?;
    let has_path_filters = !expanded_paths.is_empty();
    let fetch_limit = if has_path_filters {
        fts::FILTERED_FETCH_CAP
    } else {
        ((options.offset + options.limit) * 3).max(200)
    };
    let all_results = fts::search_content_visible(
        &mut conn,
        query,
        ctx,
        options.language,
        &expanded_paths,
        fetch_limit,
    )?;
    let cap_hint = (has_path_filters && all_results.len() >= fts::FILTERED_FETCH_CAP)
        .then(filtered_fetch_cap_hint);
    let path_hint =
        fts::path_filter_requires_post_filter(&expanded_paths).then(path_filter_post_filter_hint);
    let hint = token_budget::combine_hints(cap_hint, path_hint);
    let all_results: Vec<_> = all_results
        .into_iter()
        .filter(|r| {
            options
                .language
                .is_none_or(|lang| r.language.as_deref() == Some(lang))
                && path_matches_filters(&path_patterns, &r.file_path)
        })
        .collect();
    let all_results = retain_indexed_results(&mut conn, ctx, all_results, |r| r.file_path.as_str());
    let total = if has_path_filters {
        all_results.len()
    } else {
        fts::count_content_visible(&mut conn, query, ctx, options.language, &expanded_paths)?
    };
    let results: Vec<_> = all_results
        .into_iter()
        .skip(options.offset)
        .take(options.limit)
        .collect();

    output::print_empty_diagnostic(ctx, results.is_empty(), options.offset, total, None);
    let meta = output::SearchPageMeta {
        project_id: &ctx.project_id,
        total,
        offset: options.offset,
        limit: options.limit,
        hint: hint.as_deref(),
        warnings: &[],
    };
    let page = output::paginate(
        results,
        meta,
        options.token_budget,
        options.format,
        output::render_content_results,
    );
    if matches!(options.format, Format::Text) {
        output::print_search_warning(ctx, options.verbose, hint.as_deref());
    }
    output::print_page(&page, meta, options.format, output::render_content_results)
}
