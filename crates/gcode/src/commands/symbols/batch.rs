use std::collections::BTreeMap;
use std::path::Path;

use serde::Serialize;

use super::read_symbol_source;
use crate::commands::token_budget;
use crate::config::Context;
use crate::db;
use crate::models::Symbol;
use crate::output::{self, Format};
use crate::savings;
use crate::visibility;

#[derive(Clone, Debug, Serialize)]
pub(super) struct RetrievedSymbol {
    #[serde(flatten)]
    pub(super) symbol: Symbol,
    pub(super) source: Option<String>,
}

#[derive(Debug)]
pub(super) struct PreparedSymbolBatch {
    pub(super) results: Vec<RetrievedSymbol>,
    pub(super) missing_ids: Vec<String>,
    total_file_bytes: usize,
    total_symbol_bytes: usize,
}

pub(super) fn prepare_symbol_batch(
    root: &Path,
    ids: &[String],
    symbols: Vec<Symbol>,
) -> anyhow::Result<PreparedSymbolBatch> {
    let by_id = symbols
        .into_iter()
        .map(|symbol| (symbol.id.clone(), symbol))
        .collect::<BTreeMap<_, _>>();
    let mut results = Vec::with_capacity(ids.len());
    let mut missing_ids = Vec::new();
    let mut total_file_bytes = 0usize;
    let mut total_symbol_bytes = 0usize;

    for id in ids {
        let Some(symbol) = by_id.get(id).cloned() else {
            missing_ids.push(id.clone());
            continue;
        };
        let source = read_symbol_source(root, &symbol)?;
        if let Some(source) = &source {
            total_file_bytes = total_file_bytes.saturating_add(source.file_bytes);
            total_symbol_bytes = total_symbol_bytes.saturating_add(source.symbol_bytes);
        }
        results.push(RetrievedSymbol {
            symbol,
            source: source.map(|source| source.text),
        });
    }

    Ok(PreparedSymbolBatch {
        results,
        missing_ids,
        total_file_bytes,
        total_symbol_bytes,
    })
}

#[derive(Serialize)]
struct SymbolBatchPage<'a> {
    project_id: &'a str,
    total: usize,
    offset: usize,
    limit: usize,
    results: &'a [RetrievedSymbol],
    missing_ids: &'a [String],
    #[serde(skip_serializing_if = "Option::is_none")]
    next_offset: Option<usize>,
    #[serde(skip_serializing_if = "is_false")]
    budget_exceeded: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    hint: Option<&'a str>,
}

fn is_false(value: &bool) -> bool {
    !*value
}

pub(super) fn missing_symbols_hint(missing_ids: &[String]) -> Option<String> {
    (!missing_ids.is_empty()).then(|| {
        format!(
            "Symbols not found in the current project: {}. Edited files invalidate \
             content-derived symbol IDs; rerun `gcode outline <file> --verbose` or use \
             `gcode symbol-at <path:line>`.",
            missing_ids.join(", ")
        )
    })
}

pub(super) fn render_symbol_batch_json(
    results: &[RetrievedSymbol],
    missing_ids: &[String],
    meta: token_budget::CollectionPageMeta<'_>,
    next_offset: Option<usize>,
    budget_exceeded: bool,
    hint: Option<&str>,
) -> String {
    serde_json::to_string(&SymbolBatchPage {
        project_id: meta.project_id,
        total: meta.total,
        offset: meta.offset,
        limit: meta.limit,
        results,
        missing_ids,
        next_offset,
        budget_exceeded,
        hint,
    })
    .expect("derived symbol batch serialization cannot fail")
}

pub fn symbols(
    ctx: &Context,
    ids: &[String],
    limit: Option<usize>,
    offset: usize,
    token_budget: Option<usize>,
    format: Format,
) -> anyhow::Result<()> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let symbols = visibility::visible_symbols_by_ids(&mut conn, ctx, ids)?;
    let batch = prepare_symbol_batch(&ctx.project_root, ids, symbols)?;

    if batch.total_symbol_bytes > 0 && batch.total_file_bytes > batch.total_symbol_bytes {
        let url = gobby_core::daemon_url::daemon_url();
        savings::report_savings(
            &url,
            batch.total_file_bytes,
            batch.total_symbol_bytes,
            ctx.grant_ai.as_ref().map(|grant| &grant.bundle),
        );
    }

    let (total, limit, results) = token_budget::window(batch.results, offset, limit);
    let meta = token_budget::CollectionPageMeta {
        project_id: &ctx.project_id,
        total,
        offset,
        limit,
        hint: None,
    };
    let hint = missing_symbols_hint(&batch.missing_ids);
    let has_more = total > offset.saturating_add(results.len());
    let page = token_budget::paginate_results(
        results,
        offset,
        has_more,
        token_budget,
        |rows, next_offset, budget_exceeded| match format {
            Format::Json => render_symbol_batch_json(
                rows,
                &batch.missing_ids,
                meta,
                next_offset,
                budget_exceeded,
                hint.as_deref(),
            ),
            Format::Text => token_budget::render_text_page(
                &render_symbols_text(rows, &batch.missing_ids),
                next_offset,
            ),
        },
    );
    let rendered = match format {
        Format::Json => render_symbol_batch_json(
            &page.results,
            &batch.missing_ids,
            meta,
            page.next_offset,
            page.budget_exceeded,
            hint.as_deref(),
        ),
        Format::Text => token_budget::render_text_page(
            &render_symbols_text(&page.results, &batch.missing_ids),
            page.next_offset,
        ),
    };
    if rendered.is_empty() {
        Ok(())
    } else {
        output::print_text(&rendered)
    }
}

pub(super) fn render_symbols_text(symbols: &[RetrievedSymbol], missing_ids: &[String]) -> String {
    let mut blocks = symbols
        .iter()
        .map(|symbol| {
            let source = symbol
                .source
                .as_deref()
                .unwrap_or("[source file not found on disk]");
            format!(
                "{}:{}-{} [{}] {}\n{}",
                symbol.symbol.file_path,
                symbol.symbol.line_start,
                symbol.symbol.line_end,
                symbol.symbol.kind,
                symbol.symbol.qualified_name,
                source,
            )
        })
        .collect::<Vec<_>>();
    if let Some(hint) = missing_symbols_hint(missing_ids) {
        blocks.push(hint);
    }
    blocks.join("\n\n")
}
