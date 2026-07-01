use crate::models::Symbol;

use super::text::{is_identifier_continue, is_identifier_start};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum CallSyntaxKind {
    Bare,
    Member,
    Other,
}

/// Return the deepest symbol enclosing `byte_offset`.
///
/// Parser output is ordered by byte range, with nested symbols following their
/// parents, so `rfind` picks the innermost match. Tree-sitter byte ranges are
/// end-exclusive.
pub(super) fn enclosing_symbol(symbols: &[Symbol], byte_offset: usize) -> Option<&Symbol> {
    symbols
        .iter()
        .rfind(|s| s.byte_start <= byte_offset && byte_offset < s.byte_end)
}

pub(super) fn call_syntax_kind(
    name_node: tree_sitter::Node,
    call_node: tree_sitter::Node,
) -> CallSyntaxKind {
    let Some(mut ancestor) = name_node.parent() else {
        return CallSyntaxKind::Other;
    };
    if ancestor.id() == call_node.id() {
        return CallSyntaxKind::Bare;
    }

    loop {
        if is_memberish_kind(ancestor.kind()) {
            return CallSyntaxKind::Member;
        }
        if ancestor.id() == call_node.id() {
            return CallSyntaxKind::Other;
        }
        let Some(parent) = ancestor.parent() else {
            return CallSyntaxKind::Other;
        };
        ancestor = parent;
    }
}

fn is_memberish_kind(kind: &str) -> bool {
    matches!(
        kind,
        "attribute"
            | "member_expression"
            | "selector_expression"
            | "field_expression"
            | "member_access_expression"
            | "member_call_expression"
            | "navigation_expression"
            | "scoped_identifier"
            | "scoped_call_expression"
            | "dot"
    )
}

fn is_callable_kind(kind: &str) -> bool {
    matches!(kind, "function" | "method" | "class")
}

fn resolve_same_file_callee(
    symbols: &[Symbol],
    caller_symbol: Option<&Symbol>,
    callee_name: &str,
    qualifier_path: Option<&str>,
    syntax: CallSyntaxKind,
) -> Option<String> {
    match syntax {
        CallSyntaxKind::Bare => unique_symbol_id(
            symbols
                .iter()
                .filter(|symbol| symbol.name == callee_name && is_callable_kind(&symbol.kind)),
        ),
        CallSyntaxKind::Member => {
            resolve_same_file_associated_callee(symbols, callee_name, qualifier_path).or_else(
                || {
                    let parent_symbol_id =
                        caller_symbol.and_then(|symbol| symbol.parent_symbol_id.as_deref())?;
                    unique_symbol_id(symbols.iter().filter(|symbol| {
                        symbol.name == callee_name
                            && symbol.kind == "method"
                            && symbol.parent_symbol_id.as_deref() == Some(parent_symbol_id)
                    }))
                },
            )
        }
        CallSyntaxKind::Other => None,
    }
}

pub(super) fn resolve_same_file_callee_for_language(
    language: &str,
    symbols: &[Symbol],
    caller_symbol: Option<&Symbol>,
    callee_name: &str,
    qualifier_path: Option<&str>,
    syntax: CallSyntaxKind,
) -> Option<String> {
    if language == "ruby" && syntax == CallSyntaxKind::Bare {
        // Ruby bare calls are dynamic dispatch/open-class territory; same-file
        // name matching creates noisy false edges here.
        return None;
    }
    resolve_same_file_callee(symbols, caller_symbol, callee_name, qualifier_path, syntax)
}

fn unique_symbol_id<'a>(symbols: impl Iterator<Item = &'a Symbol>) -> Option<String> {
    let mut symbols = symbols;
    let first = symbols.next()?;
    if symbols.next().is_some() {
        None
    } else {
        Some(first.id.clone())
    }
}

fn resolve_same_file_associated_callee(
    symbols: &[Symbol],
    callee_name: &str,
    qualifier_path: Option<&str>,
) -> Option<String> {
    let qualifier = qualifier_path?;
    let parent_symbol_ids = symbols
        .iter()
        .filter(|symbol| {
            symbol.name == qualifier && matches!(symbol.kind.as_str(), "class" | "type")
        })
        .map(|symbol| symbol.id.as_str())
        .collect::<Vec<_>>();
    unique_symbol_id(symbols.iter().filter(|symbol| {
        symbol.name == callee_name
            && symbol.kind == "method"
            && symbol
                .parent_symbol_id
                .as_deref()
                .is_some_and(|parent_id| parent_symbol_ids.contains(&parent_id))
    }))
}

pub(super) fn member_qualifier_path(
    language: &str,
    source: &[u8],
    call_node: tree_sitter::Node,
    name_node: tree_sitter::Node,
) -> Option<String> {
    let prefix = String::from_utf8_lossy(&source[call_node.start_byte()..name_node.start_byte()]);
    let prefix = prefix.trim();
    if prefix.starts_with('$') || prefix.contains("->") {
        return None;
    }
    // A member qualifier is immediately followed by a path separator before the
    // callee (`C.m`, `a::b`, `\Foo\bar`). When the text right before the name is
    // not a separator — e.g. the `new` keyword in `new C()` — there is no
    // qualifier and the call is bare.
    if !prefix.ends_with(['.', ':', '\\']) {
        return None;
    }
    let is_absolute_namespace = prefix.starts_with('\\');
    let full_qualifier = prefix
        .trim_end_matches(|ch: char| ch == '.' || ch == ':' || ch == '\\' || ch.is_whitespace())
        .trim();
    if language == "lua"
        && full_qualifier.starts_with("require")
        && full_qualifier.contains(['"', '\''])
    {
        return Some(full_qualifier.to_string());
    }

    let mut chars = prefix
        .trim_end_matches(|ch: char| ch == '.' || ch == ':' || ch == '\\' || ch.is_whitespace())
        .chars()
        .skip_while(|c| !is_identifier_start(*c));
    let first = chars.next()?;

    let mut qualifier = if is_absolute_namespace {
        "\\".to_string()
    } else {
        String::new()
    };
    qualifier.push(first);
    for ch in chars {
        if is_identifier_continue(ch) || matches!(ch, '.' | ':' | '\\') {
            qualifier.push(ch);
        } else {
            break;
        }
    }

    let qualifier = qualifier.trim_end_matches(['.', ':', '\\']).to_string();
    if qualifier.is_empty() {
        None
    } else {
        Some(qualifier)
    }
}

pub(in crate::index::parser) fn call_qualifier_path(
    qualifier_from_name: Option<String>,
    qualifier_from_member: impl FnOnce() -> Option<String>,
) -> Option<String> {
    qualifier_from_name.or_else(qualifier_from_member)
}

pub(in crate::index::parser) fn split_qualified_callee(raw: &str) -> (String, Option<String>) {
    let raw = raw.trim();
    for separator in ["::", "\\", "."] {
        if let Some((qualifier, name)) = raw.rsplit_once(separator)
            && !qualifier.is_empty()
            && !name.is_empty()
        {
            return (name.to_string(), Some(qualifier.to_string()));
        }
    }
    (raw.to_string(), None)
}

pub(super) fn qualifier_root_alias(qualifier: &str) -> Option<&str> {
    qualifier
        .trim_start_matches('\\')
        .split(['.', ':', '\\'])
        .find(|part| !part.is_empty())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::index::languages;
    use streaming_iterator::StreamingIterator;
    use tree_sitter::{Query, QueryCursor};

    #[test]
    fn quoted_require_member_qualifier_is_lua_only() {
        let source: &[u8] = br#"require("./utils").helper();"#;
        let ts_lang = languages::get_ts_language("javascript").expect("javascript language");
        let mut parser = tree_sitter::Parser::new();
        parser
            .set_language(&ts_lang)
            .expect("set javascript language");
        let tree = parser.parse(source, None).expect("parse javascript");
        let query = Query::new(
            &ts_lang,
            "(call_expression function: (member_expression property: (property_identifier) @name)) @call",
        )
        .expect("compile query");
        let mut cursor = QueryCursor::new();
        let mut matches = cursor.matches(&query, tree.root_node(), source);
        let capture_names = query.capture_names();
        let name_capture = capture_names
            .iter()
            .position(|name| *name == "name")
            .expect("name capture");
        let call_capture = capture_names
            .iter()
            .position(|name| *name == "call")
            .expect("call capture");
        let query_match = matches.next().expect("query match");
        let mut name_node = None;
        let mut call_node = None;
        for cap in query_match.captures {
            let capture_index = cap.index as usize;
            if capture_index == name_capture {
                name_node = Some(cap.node);
            } else if capture_index == call_capture {
                call_node = Some(cap.node);
            }
        }
        let call_node = call_node.expect("call node");
        let name_node = name_node.expect("name node");

        assert_eq!(
            member_qualifier_path("lua", source, call_node, name_node).as_deref(),
            Some(r#"require("./utils")"#)
        );
        assert_eq!(
            member_qualifier_path("javascript", source, call_node, name_node).as_deref(),
            Some("require")
        );
    }
}
