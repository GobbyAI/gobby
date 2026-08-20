//! Explicit heritage extraction (extends / implements / embeds / mixins).

use anyhow::Context as _;
use streaming_iterator::StreamingIterator;
use tree_sitter::{Query, QueryCursor};

use crate::index::languages;
use crate::models::{
    CallTargetKind, HeritageKind, InheritanceRelation, LOCAL_IMPORT_CANDIDATE_SEP, Symbol,
};

use super::calls::{
    CallExtractionContext, CallSite, CallSyntaxKind, materialize_call, split_qualified_callee,
};

pub(crate) fn extract_inheritance(
    tree: &tree_sitter::Tree,
    source: &[u8],
    spec: &languages::LanguageSpec,
    ctx: &CallExtractionContext<'_>,
    content_hash: &str,
) -> anyhow::Result<Vec<InheritanceRelation>> {
    if spec.inheritance_query.trim().is_empty() {
        return Ok(Vec::new());
    }

    let query = Query::new(ctx.ts_lang, spec.inheritance_query).with_context(|| {
        format!(
            "failed to compile inheritance query for language `{}` while parsing {}",
            ctx.language,
            ctx.file_path.display()
        )
    })?;

    let mut cursor = QueryCursor::new();
    let mut matches = cursor.matches(&query, tree.root_node(), source);
    let capture_names = query.capture_names();
    let mut rows = Vec::new();

    while let Some(m) = matches.next() {
        let mut source_node = None;
        let mut target_node = None;
        let mut kind_tag = KindTag::None;
        for cap in m.captures {
            let name = capture_names[cap.index as usize];
            match name {
                "source" => source_node = Some(cap.node),
                "target" => target_node = Some(cap.node),
                "extends" => kind_tag = KindTag::Extends,
                "implements" => kind_tag = KindTag::Implements,
                "inherits" => kind_tag = KindTag::Inherits,
                "class_base" | "colon_base" => kind_tag = KindTag::ColonBase,
                "mixin" => kind_tag = KindTag::Implements,
                _ => {}
            }
        }
        let Some(source_n) = source_node else {
            continue;
        };
        let Some(target_n) = target_node else {
            continue;
        };
        let source_raw = node_text(source, source_n);
        let target_raw = node_text(source, target_n);
        let source_display = type_leaf_name(&source_raw);
        let target_display = type_leaf_name(&target_raw);
        if source_display.is_empty() || target_display.is_empty() {
            continue;
        }
        if SKIP_TYPE_NAMES.contains(&source_display.as_str())
            || SKIP_TYPE_NAMES.contains(&target_display.as_str())
        {
            continue;
        }

        let line = target_n.start_position().row + 1;
        let (source_symbol_id, source_name, source_kind, source_external_module) =
            resolve_endpoint(source, ctx, &source_raw, source_n.start_byte(), line)?;
        let (target_symbol_id, target_name, target_kind, target_external_module) =
            resolve_endpoint(source, ctx, &target_raw, target_n.start_byte(), line)?;
        if source_name.is_empty() || target_name.is_empty() {
            continue;
        }
        let heritage_kind = heritage_kind_for(
            ctx.language,
            kind_tag,
            target_kind,
            target_symbol_id.as_deref(),
            ctx.symbols,
        );
        rows.push(InheritanceRelation {
            source_symbol_id,
            source_name,
            source_kind,
            source_external_module,
            target_symbol_id,
            target_name,
            target_kind,
            target_external_module,
            heritage_kind,
            file_path: ctx.rel_path.to_string(),
            content_hash: content_hash.to_string(),
            line,
        });
    }

    Ok(rows)
}

#[derive(Clone, Copy)]
enum KindTag {
    None,
    Extends,
    Implements,
    Inherits,
    ColonBase,
}

const SKIP_TYPE_NAMES: &[&str] = &[
    "public",
    "private",
    "protected",
    "virtual",
    "override",
    "final",
    "where",
    "internal",
];

fn heritage_kind_for(
    language: &str,
    tag: KindTag,
    target_kind: CallTargetKind,
    target_symbol_id: Option<&str>,
    symbols: &[Symbol],
) -> HeritageKind {
    match tag {
        KindTag::Extends => HeritageKind::Extends,
        KindTag::Implements => HeritageKind::Implements,
        KindTag::Inherits => HeritageKind::Inherits,
        KindTag::ColonBase => colon_base_kind(target_kind, target_symbol_id, symbols),
        KindTag::None => match language {
            "python" | "javascript" | "ruby" => HeritageKind::Inherits,
            "go" => HeritageKind::Extends,
            _ => HeritageKind::Inherits,
        },
    }
}

fn colon_base_kind(
    target_kind: CallTargetKind,
    target_symbol_id: Option<&str>,
    symbols: &[Symbol],
) -> HeritageKind {
    if target_kind != CallTargetKind::Symbol {
        return HeritageKind::Inherits;
    }
    let Some(symbol_id) = target_symbol_id else {
        return HeritageKind::Inherits;
    };
    let Some(symbol) = symbols.iter().find(|symbol| symbol.id == symbol_id) else {
        return HeritageKind::Inherits;
    };
    match symbol.kind.as_str() {
        "class" => HeritageKind::Extends,
        "type" => HeritageKind::Implements,
        _ => HeritageKind::Inherits,
    }
}

fn resolve_endpoint(
    source: &[u8],
    ctx: &CallExtractionContext<'_>,
    raw: &str,
    byte: usize,
    line: usize,
) -> anyhow::Result<(Option<String>, String, CallTargetKind, Option<String>)> {
    let stripped = strip_type_args(raw);
    let (name, qualifier) = split_qualified_callee(&stripped);
    if name.is_empty() {
        return Ok((None, String::new(), CallTargetKind::Unresolved, None));
    }
    if qualifier.is_none()
        && let Some(symbol) = unique_type_symbol(ctx.symbols, &name)
    {
        return Ok((Some(symbol.id.clone()), name, CallTargetKind::Symbol, None));
    }
    let syntax = if qualifier.is_some() {
        CallSyntaxKind::Member
    } else {
        CallSyntaxKind::Bare
    };
    let call = materialize_call(
        source,
        ctx,
        CallSite {
            callee_name: name.clone(),
            qualifier_path: qualifier,
            name_byte: byte,
            scope_byte: byte,
            line,
            syntax,
        },
        None,
    )?;
    // Heritage endpoints carry candidate files only; call-side resolution
    // markers (default export, type member) never apply to a base type.
    let carrier = if call.callee_target_kind == CallTargetKind::LocalImport {
        Some(
            call.local_import_candidate_files()
                .join(LOCAL_IMPORT_CANDIDATE_SEP),
        )
    } else {
        call.callee_external_module
    };
    Ok((
        call.callee_symbol_id.filter(|id| !id.is_empty()),
        call.callee_name,
        call.callee_target_kind,
        carrier,
    ))
}

fn unique_type_symbol<'a>(symbols: &'a [Symbol], name: &str) -> Option<&'a Symbol> {
    let mut found = None;
    for symbol in symbols {
        if symbol.name == name && matches!(symbol.kind.as_str(), "class" | "type") {
            if found.is_some() {
                return None;
            }
            found = Some(symbol);
        }
    }
    found
}

fn node_text(source: &[u8], node: tree_sitter::Node<'_>) -> String {
    String::from_utf8_lossy(&source[node.start_byte()..node.end_byte()]).into_owned()
}

fn strip_type_args(raw: &str) -> String {
    let trimmed = raw.trim();
    let cut = trimmed
        .find(['<', '[', '('])
        .map(|idx| &trimmed[..idx])
        .unwrap_or(trimmed)
        .trim();
    cut.trim_start_matches(['*', '&']).trim().to_string()
}

fn type_leaf_name(raw: &str) -> String {
    let stripped = strip_type_args(raw);
    split_qualified_callee(&stripped).0
}
