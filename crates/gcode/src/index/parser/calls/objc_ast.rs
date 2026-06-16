use anyhow::Context as _;
use streaming_iterator::StreamingIterator;
use tree_sitter::{Query, QueryCursor};

use crate::index::languages;
use crate::index::semantic::SemanticCallResolver;
use crate::models::CallRelation;

use super::resolution::{
    CallSyntaxKind, call_qualifier_path, call_syntax_kind, member_qualifier_path,
    split_qualified_callee,
};
use super::text::{is_identifier_continue, is_identifier_start, should_ignore_call_name};
use super::{CallExtractionContext, CallSite, materialize_call};

pub(super) fn extract_objc_calls(
    tree: &tree_sitter::Tree,
    source: &[u8],
    spec: &languages::LanguageSpec,
    ctx: CallExtractionContext<'_>,
    mut semantic_resolver: Option<&mut (dyn SemanticCallResolver + '_)>,
) -> anyhow::Result<Vec<CallRelation>> {
    if spec.call_query.trim().is_empty() {
        return Ok(Vec::new());
    }

    let query = Query::new(ctx.ts_lang, spec.call_query).with_context(|| {
        format!(
            "failed to compile call query for language `{}` while parsing {}",
            ctx.language,
            ctx.file_path.display()
        )
    })?;

    let mut cursor = QueryCursor::new();
    let mut matches = cursor.matches(&query, tree.root_node(), source);
    let capture_names = query.capture_names();
    let name_capture = capture_names.iter().position(|name| *name == "name");
    let call_capture = capture_names.iter().position(|name| *name == "call");
    let receiver_capture = capture_names.iter().position(|name| *name == "receiver");
    let mut calls = Vec::new();

    while let Some(m) = matches.next() {
        let mut name_node = None;
        let mut call_node = None;
        let mut receiver_node = None;

        for cap in m.captures {
            let capture_index = cap.index as usize;
            if name_capture == Some(capture_index) {
                if name_node
                    .map(|node: tree_sitter::Node| cap.node.start_byte() < node.start_byte())
                    .unwrap_or(true)
                {
                    name_node = Some(cap.node);
                }
            } else if call_capture == Some(capture_index) {
                call_node = Some(cap.node);
            } else if receiver_capture == Some(capture_index) {
                receiver_node = Some(cap.node);
            }
        }

        let Some(name_n) = name_node else {
            continue;
        };
        let target = call_node.unwrap_or(name_n);
        if target.kind() == "message_expression"
            && target
                .child_by_field_name("method")
                .is_some_and(|first_method| first_method.id() != name_n.id())
        {
            continue;
        }

        let raw_callee =
            String::from_utf8_lossy(&source[name_n.start_byte()..name_n.end_byte()]).to_string();
        let (callee_name, qualifier_from_name) = split_qualified_callee(&raw_callee);
        if should_ignore_call_name(ctx.language, &callee_name) {
            continue;
        }

        let (qualifier_path, syntax) = if target.kind() == "message_expression" {
            (
                receiver_node.and_then(|receiver| {
                    objc_message_receiver_qualifier(source, target.start_byte(), receiver)
                }),
                CallSyntaxKind::Member,
            )
        } else {
            let qualifier_path = call_qualifier_path(qualifier_from_name, || {
                member_qualifier_path(ctx.language, source, target, name_n)
            });
            let detected_syntax = call_syntax_kind(name_n, target);
            let syntax = if qualifier_path.is_some() && detected_syntax == CallSyntaxKind::Bare {
                CallSyntaxKind::Member
            } else {
                detected_syntax
            };
            (qualifier_path, syntax)
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

fn objc_message_receiver_qualifier(
    source: &[u8],
    call_start: usize,
    receiver: tree_sitter::Node,
) -> Option<String> {
    let receiver_text =
        String::from_utf8_lossy(&source[receiver.start_byte()..receiver.end_byte()]);
    let receiver_text = receiver_text.trim();
    if !is_objc_identifier(receiver_text) || matches!(receiver_text, "self" | "super") {
        return None;
    }
    if receiver_text
        .chars()
        .next()
        .is_some_and(|first| first.is_ascii_uppercase())
    {
        return Some(receiver_text.to_string());
    }
    objc_variable_type_before(source, call_start, receiver_text)
}

fn objc_variable_type_before(source: &[u8], call_start: usize, variable: &str) -> Option<String> {
    let prefix = String::from_utf8_lossy(&source[..call_start.min(source.len())]);
    for line in prefix.lines().rev().take(80) {
        if let Some(type_name) = objc_variable_type_from_line(line, variable) {
            return Some(type_name);
        }
    }
    None
}

fn objc_variable_type_from_line(line: &str, variable: &str) -> Option<String> {
    let code = line.split("//").next().unwrap_or(line);
    for (idx, _) in code.match_indices(variable) {
        if !is_identifier_boundary(code, idx, variable.len()) {
            continue;
        }
        let before = code[..idx].trim_end_matches(|ch: char| {
            ch.is_whitespace() || matches!(ch, '*' | '<' | '>' | '(' | ')' | ',' | ';')
        });
        let candidate = before
            .rsplit(|ch: char| !is_identifier_continue(ch))
            .find(|part| !part.is_empty())?;
        if is_objc_type_identifier(candidate) {
            return Some(candidate.to_string());
        }
    }
    None
}

fn is_identifier_boundary(text: &str, start: usize, len: usize) -> bool {
    let before = text[..start]
        .chars()
        .next_back()
        .is_none_or(|ch| !is_identifier_continue(ch));
    let after = text[start + len..]
        .chars()
        .next()
        .is_none_or(|ch| !is_identifier_continue(ch));
    before && after
}

fn is_objc_type_identifier(name: &str) -> bool {
    is_objc_identifier(name)
        && name
            .chars()
            .next()
            .is_some_and(|first| first.is_ascii_uppercase())
}

fn is_objc_identifier(name: &str) -> bool {
    let mut chars = name.chars();
    let Some(first) = chars.next() else {
        return false;
    };
    (is_identifier_start(first) || first == '_') && chars.all(is_identifier_continue)
}
