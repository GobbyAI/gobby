use std::path::Path;

use streaming_iterator::StreamingIterator;
use tree_sitter::{Query, QueryCursor};

use crate::index::import_resolution::{self, ImportBindings};
use crate::index::languages;
use crate::index::semantic::{SemanticCallRequest, SemanticCallResolver};
use crate::models::{CallRelation, Symbol};

use super::ImportResolutionContext;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum CallSyntaxKind {
    Bare,
    Member,
    Other,
}

pub(super) struct CallExtractionContext<'a> {
    pub(super) language: &'a str,
    pub(super) ts_lang: &'a tree_sitter::Language,
    pub(super) rel_path: &'a str,
    pub(super) symbols: &'a [Symbol],
    pub(super) import_context: &'a ImportResolutionContext,
    pub(super) import_bindings: &'a ImportBindings,
    pub(super) file_path: &'a Path,
    pub(super) root_path: &'a Path,
}

pub(super) fn extract_calls(
    tree: &tree_sitter::Tree,
    source: &[u8],
    spec: &languages::LanguageSpec,
    ctx: CallExtractionContext<'_>,
    mut semantic_resolver: Option<&mut (dyn SemanticCallResolver + '_)>,
) -> anyhow::Result<Vec<CallRelation>> {
    let language = ctx.language;
    let rel_path = ctx.rel_path;
    let symbols = ctx.symbols;
    let import_context = ctx.import_context;
    let import_bindings = ctx.import_bindings;
    if language == "dart" {
        return extract_textual_dart_calls(source, ctx, semantic_resolver);
    }
    if spec.call_query.trim().is_empty() {
        return Ok(Vec::new());
    }

    let query = match Query::new(ctx.ts_lang, spec.call_query) {
        Ok(q) => q,
        Err(_) => return Ok(Vec::new()),
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
        let caller_symbol = enclosing_symbol(symbols, target.start_byte());
        let caller_symbol_id = caller_symbol.map(|s| s.id.clone()).unwrap_or_default();
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
        let local_target = resolve_same_file_callee_for_language(
            language,
            symbols,
            caller_symbol,
            &callee_name,
            syntax,
        );
        let root_alias = qualifier_path
            .as_deref()
            .and_then(qualifier_root_alias)
            .map(ToOwned::to_owned);
        let external_shadowed = external_call_is_shadowed(
            source,
            caller_symbol,
            target.start_byte(),
            &callee_name,
            root_alias.as_deref(),
            syntax,
        );
        let external_target = if external_shadowed {
            None
        } else {
            import_resolution::resolve_external_callee(
                import_context,
                import_bindings,
                symbols,
                &callee_name,
                root_alias.as_deref(),
                qualifier_path.as_deref(),
                syntax == CallSyntaxKind::Bare,
            )
        };
        let semantic_target =
            if local_target.is_none() && external_target.is_none() && !external_shadowed {
                if let Some(resolver) = semantic_resolver.as_deref_mut() {
                    resolver.resolve(&SemanticCallRequest {
                        language,
                        file_path: ctx.file_path,
                        root_path: ctx.root_path,
                        source,
                        callee_name: &callee_name,
                        line: name_n.start_position().row + 1,
                        column: utf16_column_at_byte(source, name_n.start_byte()),
                    })?
                } else {
                    None
                }
            } else {
                None
            };

        let mut call = CallRelation::new(
            caller_symbol_id,
            callee_name,
            rel_path.to_string(),
            name_n.start_position().row + 1,
        );
        match (local_target, external_target) {
            (Some(callee_symbol_id), None) => {
                call = call.with_symbol_target(callee_symbol_id);
            }
            (None, Some(external_target)) => {
                call =
                    call.with_external_target(external_target.callee_name, external_target.module);
            }
            (None, None) => {
                if let Some(semantic_target) = semantic_target {
                    call = call.with_external_target(
                        semantic_target.callee_name,
                        semantic_target.external_module,
                    );
                }
            }
            _ => {}
        }
        calls.push(call);
    }

    Ok(calls)
}

fn extract_textual_dart_calls(
    source: &[u8],
    ctx: CallExtractionContext<'_>,
    mut semantic_resolver: Option<&mut (dyn SemanticCallResolver + '_)>,
) -> anyhow::Result<Vec<CallRelation>> {
    let rel_path = ctx.rel_path;
    let symbols = ctx.symbols;
    let import_context = ctx.import_context;
    let import_bindings = ctx.import_bindings;
    let file_path = ctx.file_path;
    let root_path = ctx.root_path;
    let text = String::from_utf8_lossy(source);
    let mut calls = Vec::new();
    let mut line_start_byte = 0usize;
    let mut dart_state = DartScanState::default();

    for (row, line) in text.lines().enumerate() {
        let terminator_len = line_terminator_len(&text, line_start_byte, line.len());
        let trimmed = line.trim_start();
        if dart_state.is_code()
            && (trimmed.starts_with("import ")
                || trimmed.starts_with("export ")
                || trimmed.starts_with("class ")
                || trimmed.starts_with("enum ")
                || trimmed.starts_with("typedef "))
        {
            dart_state = dart_state_after_line(line, dart_state);
            line_start_byte += line.len() + terminator_len;
            continue;
        }

        for candidate in textual_call_candidates(line, line_start_byte, &['.']) {
            let candidate_line_byte = candidate.call_byte.saturating_sub(line_start_byte);
            if dart_textual_candidate_in_ignored_context(line, candidate_line_byte, dart_state) {
                continue;
            }
            if should_ignore_call_name("dart", &candidate.name) {
                continue;
            }
            let caller_symbol = enclosing_symbol(symbols, candidate.call_byte);
            let caller_symbol_id = caller_symbol.map(|s| s.id.clone()).unwrap_or_default();
            let syntax = if candidate.qualifier_path.is_some() {
                CallSyntaxKind::Member
            } else {
                CallSyntaxKind::Bare
            };
            let local_target = resolve_same_file_callee_for_language(
                "dart",
                symbols,
                caller_symbol,
                &candidate.name,
                syntax,
            );
            let root_alias = candidate
                .qualifier_path
                .as_deref()
                .and_then(qualifier_root_alias)
                .map(ToOwned::to_owned);
            let external_shadowed = external_call_is_shadowed(
                source,
                caller_symbol,
                candidate.call_byte,
                &candidate.name,
                root_alias.as_deref(),
                syntax,
            );
            let external_target = if external_shadowed {
                None
            } else {
                import_resolution::resolve_external_callee(
                    import_context,
                    import_bindings,
                    symbols,
                    &candidate.name,
                    root_alias.as_deref(),
                    candidate.qualifier_path.as_deref(),
                    syntax == CallSyntaxKind::Bare,
                )
            };
            let semantic_target =
                if local_target.is_none() && external_target.is_none() && !external_shadowed {
                    if let Some(resolver) = semantic_resolver.as_deref_mut() {
                        resolver.resolve(&SemanticCallRequest {
                            language: "dart",
                            file_path,
                            root_path,
                            source,
                            callee_name: &candidate.name,
                            line: row + 1,
                            column: utf16_column_at_byte(source, candidate.call_byte),
                        })?
                    } else {
                        None
                    }
                } else {
                    None
                };

            let mut call = CallRelation::new(
                caller_symbol_id,
                candidate.name,
                rel_path.to_string(),
                row + 1,
            );
            match (local_target, external_target) {
                (Some(callee_symbol_id), None) => {
                    call = call.with_symbol_target(callee_symbol_id);
                }
                (None, Some(external_target)) => {
                    call = call
                        .with_external_target(external_target.callee_name, external_target.module);
                }
                (None, None) => {
                    if let Some(semantic_target) = semantic_target {
                        call = call.with_external_target(
                            semantic_target.callee_name,
                            semantic_target.external_module,
                        );
                    }
                }
                _ => {}
            }
            calls.push(call);
        }

        dart_state = dart_state_after_line(line, dart_state);
        line_start_byte += line.len() + terminator_len;
    }

    Ok(calls)
}

pub(super) fn line_terminator_len(text: &str, line_start_byte: usize, line_len: usize) -> usize {
    let terminator_start = line_start_byte + line_len;
    let Some(rest) = text.as_bytes().get(terminator_start..) else {
        return 0;
    };
    if rest.starts_with(b"\r\n") {
        2
    } else if rest.starts_with(b"\n") {
        1
    } else {
        0
    }
}

fn utf16_column_at_byte(source: &[u8], byte_offset: usize) -> usize {
    let byte_offset = byte_offset.min(source.len());
    let line_start = source[..byte_offset]
        .iter()
        .rposition(|byte| *byte == b'\n')
        .map(|idx| idx + 1)
        .unwrap_or(0);
    String::from_utf8_lossy(&source[line_start..byte_offset])
        .encode_utf16()
        .count()
}

#[derive(Debug)]
struct TextualCallCandidate {
    name: String,
    qualifier_path: Option<String>,
    call_byte: usize,
}

fn textual_call_candidates(
    line: &str,
    line_start_byte: usize,
    separators: &[char],
) -> Vec<TextualCallCandidate> {
    let bytes = line.as_bytes();
    let mut candidates = Vec::new();
    let mut idx = 0usize;

    while idx < bytes.len() {
        if bytes[idx] != b'(' {
            idx += 1;
            continue;
        }
        let mut end = idx;
        while end > 0 && bytes[end - 1].is_ascii_whitespace() {
            end -= 1;
        }
        let (start, name_end) = if end > 0 && bytes[end - 1] == b'>' {
            let Some(generic_start) = matching_angle_start(bytes, end - 1) else {
                idx += 1;
                continue;
            };
            let mut start = generic_start;
            while start > 0 && is_textual_call_name_byte(bytes[start - 1]) {
                start -= 1;
            }
            (start, generic_start)
        } else {
            let mut start = end;
            while start > 0 && is_textual_call_name_byte(bytes[start - 1]) {
                start -= 1;
            }
            (start, end)
        };
        if start == end {
            idx += 1;
            continue;
        }

        let name = &line[start..name_end];
        if name.is_empty() {
            idx += 1;
            continue;
        }
        if looks_like_textual_function_declaration(line, start, idx) {
            idx += 1;
            continue;
        }
        let mut qualifier_path = None;
        let mut prefix_end = start;
        while prefix_end > 0 && bytes[prefix_end - 1].is_ascii_whitespace() {
            prefix_end -= 1;
        }
        if prefix_end > 0 && separators.contains(&(bytes[prefix_end - 1] as char)) {
            let mut qualifier_start = prefix_end - 1;
            while qualifier_start > 0 {
                let ch = bytes[qualifier_start - 1] as char;
                if is_identifier_continue(ch) || separators.contains(&ch) {
                    qualifier_start -= 1;
                } else {
                    break;
                }
            }
            let qualifier = line[qualifier_start..prefix_end - 1].trim();
            if !qualifier.is_empty() {
                qualifier_path = Some(qualifier.to_string());
            }
        }

        candidates.push(TextualCallCandidate {
            name: name.to_string(),
            qualifier_path,
            call_byte: line_start_byte + start,
        });
        idx += 1;
    }

    candidates
}

fn matching_angle_start(bytes: &[u8], close_idx: usize) -> Option<usize> {
    let mut depth = 0usize;
    for idx in (0..=close_idx).rev() {
        match bytes[idx] {
            b'>' => depth += 1,
            b'<' if depth > 0 => {
                depth -= 1;
                if depth == 0 {
                    return Some(idx);
                }
            }
            _ => {}
        }
    }
    None
}

#[derive(Debug, Clone, Copy, Default)]
struct DartScanState {
    in_block_comment: bool,
    string: Option<DartStringState>,
}

impl DartScanState {
    fn is_code(self) -> bool {
        !self.in_block_comment && self.string.is_none()
    }
}

#[derive(Debug, Clone, Copy)]
struct DartStringState {
    quote: u8,
    triple: bool,
    raw: bool,
    escaped: bool,
}

fn dart_textual_candidate_in_ignored_context(
    line: &str,
    candidate_byte: usize,
    state: DartScanState,
) -> bool {
    let (state, in_line_comment) = dart_scan_line_until(line, candidate_byte, state);
    in_line_comment || !state.is_code()
}

fn dart_state_after_line(line: &str, state: DartScanState) -> DartScanState {
    dart_scan_line_until(line, line.len(), state).0
}

fn dart_scan_line_until(
    line: &str,
    limit: usize,
    mut state: DartScanState,
) -> (DartScanState, bool) {
    let bytes = line.as_bytes();
    let limit = limit.min(bytes.len());
    let mut idx = 0usize;

    while idx < limit {
        if state.in_block_comment {
            if bytes[idx..].starts_with(b"*/") {
                state.in_block_comment = false;
                idx += 2;
            } else {
                idx += 1;
            }
            continue;
        }

        if let Some(mut string) = state.string {
            if string.triple
                && bytes[idx..].starts_with(&[string.quote, string.quote, string.quote])
            {
                state.string = None;
                idx += 3;
                continue;
            }
            if !string.triple {
                if !string.raw && string.escaped {
                    string.escaped = false;
                } else if !string.raw && bytes[idx] == b'\\' {
                    string.escaped = true;
                } else if bytes[idx] == string.quote {
                    state.string = None;
                    idx += 1;
                    continue;
                }
                state.string = Some(string);
            }
            idx += 1;
            continue;
        }

        if bytes[idx..].starts_with(b"//") {
            return (state, true);
        }
        if bytes[idx..].starts_with(b"/*") {
            state.in_block_comment = true;
            idx += 2;
            continue;
        }
        if let Some((string, consumed)) = dart_string_start(bytes, idx) {
            state.string = Some(string);
            idx += consumed;
            continue;
        }
        idx += 1;
    }

    (state, false)
}

fn dart_string_start(bytes: &[u8], idx: usize) -> Option<(DartStringState, usize)> {
    let (raw, quote_idx) =
        if bytes.get(idx) == Some(&b'r') && matches!(bytes.get(idx + 1), Some(b'\'' | b'"')) {
            (true, idx + 1)
        } else if matches!(bytes.get(idx), Some(b'\'' | b'"')) {
            (false, idx)
        } else {
            return None;
        };
    let quote = bytes[quote_idx];
    let triple = bytes
        .get(quote_idx..quote_idx + 3)
        .is_some_and(|slice| slice == [quote, quote, quote]);
    Some((
        DartStringState {
            quote,
            triple,
            raw,
            escaped: false,
        },
        (if raw { 1 } else { 0 }) + if triple { 3 } else { 1 },
    ))
}

fn looks_like_textual_function_declaration(
    line: &str,
    name_start: usize,
    open_paren: usize,
) -> bool {
    let prefix = line[..name_start].trim_end();
    let after_paren = &line[open_paren + 1..];
    let after_args = after_paren
        .find(')')
        .and_then(|close| after_paren.get(close + 1..))
        .unwrap_or_default()
        .trim_start();
    let has_declaration_tail = after_args.starts_with('{')
        || after_args.starts_with("=>")
        || after_args.starts_with("async")
        || after_args.starts_with("sync")
        || after_args.starts_with("external")
        || after_args.starts_with(';');
    if !has_declaration_tail {
        return false;
    }

    if prefix.is_empty() {
        return !after_args.starts_with(';');
    }
    if prefix.contains(['=', '.', '(', ',', ';']) {
        return false;
    }
    let Some(previous_token) = prefix.split_whitespace().last() else {
        return false;
    };
    previous_token.contains('<')
        || previous_token
            .chars()
            .next()
            .is_some_and(|ch| ch.is_ascii_uppercase())
        || matches!(
            previous_token,
            "void"
                | "int"
                | "double"
                | "num"
                | "String"
                | "bool"
                | "dynamic"
                | "Object"
                | "Future"
                | "Stream"
        )
}

fn enclosing_symbol(symbols: &[Symbol], byte_offset: usize) -> Option<&Symbol> {
    symbols
        .iter()
        .rfind(|s| s.byte_start <= byte_offset && byte_offset <= s.byte_end)
}

fn call_syntax_kind(name_node: tree_sitter::Node, call_node: tree_sitter::Node) -> CallSyntaxKind {
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
            | "scoped_call_expression"
            | "dot"
    )
}

fn is_callable_kind(kind: &str) -> bool {
    matches!(kind, "function" | "method")
}

fn resolve_same_file_callee(
    symbols: &[Symbol],
    caller_symbol: Option<&Symbol>,
    callee_name: &str,
    syntax: CallSyntaxKind,
) -> Option<String> {
    match syntax {
        CallSyntaxKind::Bare => unique_symbol_id(
            symbols
                .iter()
                .filter(|symbol| symbol.name == callee_name && is_callable_kind(&symbol.kind)),
        ),
        CallSyntaxKind::Member => {
            let parent_symbol_id =
                caller_symbol.and_then(|symbol| symbol.parent_symbol_id.as_deref())?;
            unique_symbol_id(symbols.iter().filter(|symbol| {
                symbol.name == callee_name
                    && symbol.kind == "method"
                    && symbol.parent_symbol_id.as_deref() == Some(parent_symbol_id)
            }))
        }
        CallSyntaxKind::Other => None,
    }
}

fn resolve_same_file_callee_for_language(
    language: &str,
    symbols: &[Symbol],
    caller_symbol: Option<&Symbol>,
    callee_name: &str,
    syntax: CallSyntaxKind,
) -> Option<String> {
    if language == "ruby" && syntax == CallSyntaxKind::Bare {
        // Ruby bare calls are dynamic dispatch/open-class territory; same-file
        // name matching creates noisy false edges here.
        return None;
    }
    resolve_same_file_callee(symbols, caller_symbol, callee_name, syntax)
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

fn member_qualifier_path(
    source: &[u8],
    call_node: tree_sitter::Node,
    name_node: tree_sitter::Node,
) -> Option<String> {
    let prefix = String::from_utf8_lossy(&source[call_node.start_byte()..name_node.start_byte()]);
    let prefix = prefix.trim();
    if prefix.starts_with('$') || prefix.contains("->") || prefix.contains("?->") {
        return None;
    }
    let is_absolute_namespace = prefix.starts_with('\\');

    let mut chars = prefix
        .trim_end_matches(|ch: char| ch == '.' || ch == ':' || ch == '\\' || ch.is_whitespace())
        .chars()
        .skip_while(|c| !is_identifier_start(*c));
    let first = chars.next()?;
    if !is_identifier_start(first) {
        return None;
    }

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

pub(super) fn call_qualifier_path(
    qualifier_from_name: Option<String>,
    qualifier_from_member: impl FnOnce() -> Option<String>,
) -> Option<String> {
    qualifier_from_name.or_else(qualifier_from_member)
}

fn external_call_is_shadowed(
    source: &[u8],
    caller_symbol: Option<&Symbol>,
    call_byte: usize,
    callee_name: &str,
    root_alias: Option<&str>,
    syntax: CallSyntaxKind,
) -> bool {
    let shadow_name = match syntax {
        CallSyntaxKind::Bare => Some(callee_name),
        CallSyntaxKind::Member => root_alias,
        CallSyntaxKind::Other => None,
    };
    let Some(shadow_name) = shadow_name.filter(|name| !name.is_empty()) else {
        return false;
    };
    local_name_in_scope_before_call(source, caller_symbol, call_byte, shadow_name)
}

fn local_name_in_scope_before_call(
    source: &[u8],
    caller_symbol: Option<&Symbol>,
    call_byte: usize,
    name: &str,
) -> bool {
    let start = caller_symbol.map(|symbol| symbol.byte_start).unwrap_or(0);
    if start >= source.len() || start >= call_byte {
        return false;
    }
    let end = call_byte.min(source.len());
    let prefix = String::from_utf8_lossy(&source[start..end]);
    caller_symbol.is_some_and(|_| parameter_list_contains_name(&prefix, name))
        || prefix
            .lines()
            .any(|line| local_binding_line_defines(line, name))
}

fn parameter_list_contains_name(prefix: &str, name: &str) -> bool {
    let Some(open) = prefix.find('(') else {
        return false;
    };
    let Some(close) = matching_paren_in_str(prefix, open) else {
        return false;
    };
    prefix[open + 1..close]
        .split(',')
        .any(|param| parameter_segment_name(param).is_some_and(|param_name| param_name == name))
}

fn matching_paren_in_str(text: &str, open: usize) -> Option<usize> {
    let mut depth = 0usize;
    for (idx, ch) in text.char_indices().skip_while(|(idx, _)| *idx < open) {
        match ch {
            '(' => depth += 1,
            ')' => {
                depth = depth.saturating_sub(1);
                if depth == 0 {
                    return Some(idx);
                }
            }
            _ => {}
        }
    }
    None
}

fn parameter_segment_name(segment: &str) -> Option<&str> {
    let segment = segment
        .split('=')
        .next()
        .unwrap_or(segment)
        .split(':')
        .next()
        .unwrap_or(segment)
        .trim();
    segment
        .split_whitespace()
        .find(|token| token.chars().next().is_some_and(is_identifier_start))
        .map(trim_identifier_token)
        .filter(|token| !token.is_empty())
}

fn local_binding_line_defines(line: &str, name: &str) -> bool {
    let line = line.trim_start();
    if line.is_empty()
        || line.starts_with("//")
        || line.starts_with('#')
        || line.starts_with("import ")
        || line.starts_with("from ")
        || line.starts_with("use ")
    {
        return false;
    }
    if let Some(left) = line.split(":=").next()
        && line.contains(":=")
        && binding_left_side_contains(left, name)
    {
        return true;
    }
    if let Some((left, _)) = split_assignment(line)
        && binding_left_side_contains(left, name)
    {
        return true;
    }
    declaration_without_assignment_contains(line, name)
}

fn split_assignment(line: &str) -> Option<(&str, &str)> {
    for (idx, ch) in line.char_indices() {
        if ch != '=' {
            continue;
        }
        let previous = line[..idx].chars().next_back();
        let next = line[idx + 1..].chars().next();
        if matches!(
            previous,
            Some('=' | '!' | '<' | '>' | ':' | '+' | '-' | '*' | '/' | '%')
        ) || matches!(next, Some('=' | '>'))
        {
            continue;
        }
        return Some((&line[..idx], &line[idx + 1..]));
    }
    None
}

fn binding_left_side_contains(left: &str, name: &str) -> bool {
    left.split(',')
        .filter_map(|part| binding_name_from_left_part(part))
        .any(|binding_name| binding_name == name)
}

fn binding_name_from_left_part(part: &str) -> Option<&str> {
    let part = part.trim();
    if part.contains(['.', '[', ']']) {
        return None;
    }
    part.split_whitespace()
        .next_back()
        .map(trim_identifier_token)
        .filter(|token| !token.is_empty())
}

fn declaration_without_assignment_contains(line: &str, name: &str) -> bool {
    let Some(rest) = line
        .strip_prefix("let ")
        .or_else(|| line.strip_prefix("const "))
        .or_else(|| line.strip_prefix("var "))
        .or_else(|| line.strip_prefix("final "))
        .or_else(|| line.strip_prefix("late "))
        .or_else(|| line.strip_prefix("val "))
        .or_else(|| line.strip_prefix("auto "))
    else {
        return false;
    };
    rest.split([',', ';'])
        .filter_map(binding_name_from_left_part)
        .any(|binding_name| binding_name == name)
}

fn trim_identifier_token(token: &str) -> &str {
    token.trim_matches(|ch: char| !is_identifier_continue(ch))
}

pub(super) fn split_qualified_callee(raw: &str) -> (String, Option<String>) {
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

fn qualifier_root_alias(qualifier: &str) -> Option<&str> {
    qualifier
        .trim_start_matches('\\')
        .split(['.', ':', '\\'])
        .find(|part| !part.is_empty())
}

fn is_identifier_start(ch: char) -> bool {
    ch.is_ascii_alphabetic() || matches!(ch, '_' | '$')
}

fn is_identifier_continue(ch: char) -> bool {
    ch.is_ascii_alphanumeric() || matches!(ch, '_' | '$')
}

fn is_textual_call_name_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'$' | b'!' | b'?')
}

fn should_ignore_call_name(language: &str, name: &str) -> bool {
    match language {
        "dart" => matches!(
            name,
            "if" | "for" | "while" | "switch" | "catch" | "assert" | "return" | "throw"
        ),
        "elixir" => matches!(
            name,
            "def" | "defp" | "defmacro" | "defmodule" | "alias" | "import" | "use" | "require"
        ),
        "kotlin" => matches!(
            name,
            "if" | "for" | "while" | "when" | "catch" | "return" | "throw"
        ),
        _ => false,
    }
}
