use streaming_iterator::StreamingIterator;
use tree_sitter::{Query, QueryCursor};

use crate::index::languages;
use crate::index::semantic::SemanticCallResolver;
use crate::models::CallRelation;

use super::resolution::{
    CallSyntaxKind, call_qualifier_path, call_syntax_kind, member_qualifier_path,
    split_qualified_callee,
};
use super::text::should_ignore_call_name;
use super::{CallExtractionContext, CallSite, materialize_call};

pub(super) fn extract_ast_calls(
    tree: &tree_sitter::Tree,
    source: &[u8],
    spec: &languages::LanguageSpec,
    ctx: CallExtractionContext<'_>,
    mut semantic_resolver: Option<&mut (dyn SemanticCallResolver + '_)>,
) -> anyhow::Result<Vec<CallRelation>> {
    let language = ctx.language;
    if spec.call_query.trim().is_empty() {
        return Ok(Vec::new());
    }

    let query = match Query::new(ctx.ts_lang, spec.call_query) {
        Ok(q) => q,
        Err(error) => {
            log::error!(
                "failed to compile call query for language `{language}` while parsing {}: {error}",
                ctx.file_path.display()
            );
            return Ok(Vec::new());
        }
    };

    let mut cursor = QueryCursor::new();
    let mut matches = cursor.matches(&query, tree.root_node(), source);
    let capture_names: Vec<String> = query
        .capture_names()
        .iter()
        .map(|s| s.to_string())
        .collect();
    let mut calls = Vec::new();

    while let Some(m) = matches.next() {
        let mut name_node = None;
        let mut call_node = None;

        for cap in m.captures {
            let cap_name = &capture_names[cap.index as usize];
            if cap_name == "name" {
                name_node = Some(cap.node);
            } else if cap_name == "call" {
                call_node = Some(cap.node);
            }
        }

        let name_n = match name_node {
            Some(n) => n,
            None => continue,
        };

        let raw_callee =
            String::from_utf8_lossy(&source[name_n.start_byte()..name_n.end_byte()]).to_string();
        let (callee_name, qualifier_from_name) = split_qualified_callee(&raw_callee);
        if should_ignore_call_name(language, &callee_name) {
            continue;
        }

        let target = call_node.unwrap_or(name_n);
        // If the captured callee is already qualified, trust that text over a
        // prefix inferred from the wider call node.
        let qualifier_path = call_qualifier_path(qualifier_from_name, || {
            member_qualifier_path(source, target, name_n)
        });
        let detected_syntax = call_syntax_kind(name_n, target);
        let syntax = if detected_syntax == CallSyntaxKind::Bare && qualifier_path.is_some() {
            CallSyntaxKind::Member
        } else {
            detected_syntax
        };
        calls.push(materialize_call(
            source,
            &ctx,
            CallSite {
                callee_name,
                qualifier_path,
                name_byte: name_n.start_byte(),
                scope_byte: target.start_byte(),
                line: name_n.start_position().row + 1,
                syntax,
            },
            semantic_resolver.as_deref_mut(),
        )?);
    }

    Ok(calls)
}
