use crate::config::Context;

use crate::graph::typed_query;
use crate::models::GraphResult;

use super::super::connection::with_optional_core_graph;
use super::super::payload::row_string_owned;
use super::relationship_queries::{
    blast_radius_query, count_callers_query, count_usages_query, find_callee_ids_batch_query,
    find_callees_batch_query, find_caller_ids_batch_query, find_caller_ids_query,
    find_callers_batch_query, find_callers_query, find_usage_ids_query, find_usages_query,
    get_imports_query,
};
use super::support::{MAX_GRAPH_LIMIT, count_from_rows, row_to_graph_result};

pub fn count_callers(ctx: &Context, symbol_id: &str) -> anyhow::Result<usize> {
    with_optional_core_graph(
        ctx,
        || 0,
        |client| {
            let (query, params) = count_callers_query(&ctx.project_id, symbol_id);
            let rows = client.query(&query, Some(params))?;
            Ok(count_from_rows(&rows))
        },
    )
}

pub fn count_usages(ctx: &Context, symbol_id: &str) -> anyhow::Result<usize> {
    with_optional_core_graph(
        ctx,
        || 0,
        |client| {
            let (query, params) = count_usages_query(&ctx.project_id, symbol_id);
            let rows = client.query(&query, Some(params))?;
            Ok(count_from_rows(&rows))
        },
    )
}

pub fn find_callers(
    ctx: &Context,
    symbol_id: &str,
    offset: usize,
    limit: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    with_optional_core_graph(ctx, Vec::new, |client| {
        let (query, params) = find_callers_query(&ctx.project_id, symbol_id, offset, limit);
        let rows = client.query(&query, Some(params))?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}

pub fn find_usages(
    ctx: &Context,
    symbol_id: &str,
    offset: usize,
    limit: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    with_optional_core_graph(ctx, Vec::new, |client| {
        let (query, params) = find_usages_query(&ctx.project_id, symbol_id, offset, limit);
        let rows = client.query(&query, Some(params))?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}

pub fn find_caller_ids(
    ctx: &Context,
    symbol_id: &str,
    limit: usize,
) -> anyhow::Result<Vec<String>> {
    with_optional_core_graph(ctx, Vec::new, |client| {
        let (query, params) = find_caller_ids_query(&ctx.project_id, symbol_id, limit);
        let rows = client.query(&query, Some(params))?;
        Ok(rows
            .iter()
            .filter_map(|row| row_string_owned(row, &["id"]))
            .collect())
    })
}

pub fn find_usage_ids(ctx: &Context, symbol_id: &str, limit: usize) -> anyhow::Result<Vec<String>> {
    with_optional_core_graph(ctx, Vec::new, |client| {
        let (query, params) = find_usage_ids_query(&ctx.project_id, symbol_id, limit);
        let rows = client.query(&query, Some(params))?;
        Ok(rows
            .iter()
            .filter_map(|row| row_string_owned(row, &["id"]))
            .collect())
    })
}

pub fn find_callers_batch(
    ctx: &Context,
    symbol_ids: &[String],
    limit: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    if symbol_ids.is_empty() {
        return Ok(vec![]);
    }
    with_optional_core_graph(ctx, Vec::new, |client| {
        let (query, params) = find_callers_batch_query(&ctx.project_id, symbol_ids, limit);
        let rows = client.query(&query, Some(params))?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}

pub fn find_caller_ids_batch(
    ctx: &Context,
    symbol_ids: &[String],
    limit: usize,
) -> anyhow::Result<Vec<String>> {
    if symbol_ids.is_empty() {
        return Ok(vec![]);
    }
    with_optional_core_graph(ctx, Vec::new, |client| {
        let (query, params) = find_caller_ids_batch_query(&ctx.project_id, symbol_ids, limit);
        let rows = client.query(&query, Some(params))?;
        Ok(rows
            .iter()
            .filter_map(|row| row_string_owned(row, &["id"]))
            .collect())
    })
}

pub fn find_callees_batch(
    ctx: &Context,
    symbol_ids: &[String],
    limit: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    if symbol_ids.is_empty() {
        return Ok(vec![]);
    }
    with_optional_core_graph(ctx, Vec::new, |client| {
        let (query, params) = find_callees_batch_query(&ctx.project_id, symbol_ids, limit);
        let rows = client.query(&query, Some(params))?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}

pub fn find_callee_ids_batch(
    ctx: &Context,
    symbol_ids: &[String],
    limit: usize,
) -> anyhow::Result<Vec<String>> {
    if symbol_ids.is_empty() {
        return Ok(vec![]);
    }
    with_optional_core_graph(ctx, Vec::new, |client| {
        let (query, params) = find_callee_ids_batch_query(&ctx.project_id, symbol_ids, limit);
        let rows = client.query(&query, Some(params))?;
        Ok(rows
            .iter()
            .filter_map(|row| row_string_owned(row, &["id"]))
            .collect())
    })
}

pub fn get_imports(ctx: &Context, file_path: &str) -> anyhow::Result<Vec<GraphResult>> {
    with_optional_core_graph(ctx, Vec::new, |client| {
        let (query, params) = get_imports_query(&ctx.project_id, file_path);
        let rows = client.query(&query, Some(params))?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}

pub fn blast_radius(
    ctx: &Context,
    symbol_id: &str,
    depth: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    with_optional_core_graph(ctx, Vec::new, |client| {
        let query = blast_radius_query(depth, MAX_GRAPH_LIMIT);
        let params = typed_query::string_params(&[("project", &ctx.project_id), ("id", symbol_id)]);
        let rows = client.query(&query, Some(params))?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}
