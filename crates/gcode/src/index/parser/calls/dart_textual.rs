use crate::index::semantic::SemanticCallResolver;
use crate::models::CallRelation;

use super::resolution::CallSyntaxKind;
use super::text::{is_textual_call_name_byte, should_ignore_call_name};
use super::{CallExtractionContext, CallSite, materialize_call};

pub(super) fn extract_textual_dart_calls(
    source: &[u8],
    ctx: CallExtractionContext<'_>,
    mut semantic_resolver: Option<&mut (dyn SemanticCallResolver + '_)>,
) -> anyhow::Result<Vec<CallRelation>> {
    let mut calls = Vec::new();
    let mut dart_state = DartScanState::default();

    for (row, (line_start_byte, line_bytes)) in source_line_spans(source).into_iter().enumerate() {
        let line = String::from_utf8_lossy(line_bytes);
        let trimmed = line.trim_start();
        if dart_state.is_code()
            && (trimmed.starts_with("import ")
                || trimmed.starts_with("export ")
                || trimmed.starts_with("class ")
                || trimmed.starts_with("enum ")
                || trimmed.starts_with("typedef "))
        {
            dart_state = dart_state_after_line(&line, dart_state);
            continue;
        }

        for candidate in textual_call_candidates(&line, line_start_byte, row + 1, b".") {
            let candidate_line_byte = candidate.name_byte.saturating_sub(line_start_byte);
            if dart_textual_candidate_in_ignored_context(
                &line,
                candidate_line_byte,
                dart_state.clone(),
            ) {
                continue;
            }
            if empty_prefix_semicolon_declaration_in_class(
                &line,
                candidate_line_byte,
                dart_state.clone(),
            ) {
                continue;
            }
            if should_ignore_call_name("dart", &candidate.callee_name) {
                continue;
            }
            calls.push(materialize_call(
                source,
                &ctx,
                candidate,
                semantic_resolver.as_deref_mut(),
            )?);
        }

        dart_state = dart_state_after_line(&line, dart_state);
    }

    Ok(calls)
}

fn source_line_spans(source: &[u8]) -> Vec<(usize, &[u8])> {
    let mut spans = Vec::new();
    let mut start = 0usize;
    while start < source.len() {
        let line_end = source[start..]
            .iter()
            .position(|byte| *byte == b'\n')
            .map_or(source.len(), |relative| start + relative);
        let content_end = if line_end > start && source[line_end - 1] == b'\r' {
            line_end - 1
        } else {
            line_end
        };
        spans.push((start, &source[start..content_end]));
        if line_end == source.len() {
            break;
        }
        start = line_end + 1;
    }
    spans
}

fn textual_call_candidates(
    line: &str,
    line_start_byte: usize,
    line_number: usize,
    separators: &[u8],
) -> Vec<CallSite> {
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
        if prefix_end > 0 && separators.contains(&bytes[prefix_end - 1]) {
            let mut qualifier_start = prefix_end - 1;
            while qualifier_start > 0 {
                let byte = bytes[qualifier_start - 1];
                if is_textual_qualifier_byte(byte) || separators.contains(&byte) {
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

        let syntax = if qualifier_path.is_some() {
            CallSyntaxKind::Member
        } else {
            CallSyntaxKind::Bare
        };
        let name_byte = line_start_byte + start;
        candidates.push(CallSite {
            callee_name: name.to_string(),
            qualifier_path,
            name_byte,
            scope_byte: name_byte,
            line: line_number,
            syntax,
        });
        idx += 1;
    }

    candidates
}

fn is_textual_qualifier_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'$')
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

#[derive(Debug, Clone, Default)]
struct DartScanState {
    in_block_comment: bool,
    string: Option<DartStringState>,
    brace_depth: usize,
    pending_class_body: bool,
    class_body_depths: Vec<usize>,
}

impl DartScanState {
    fn is_code(&self) -> bool {
        !self.in_block_comment && self.string.is_none()
    }

    fn in_class_member_scope(&self) -> bool {
        self.class_body_depths
            .last()
            .is_some_and(|depth| self.brace_depth == *depth)
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
    let mut state = state;
    if state.is_code() && dart_line_starts_type_declaration(line) {
        state.pending_class_body = true;
    }
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
        match bytes[idx] {
            b'{' => {
                state.brace_depth = state.brace_depth.saturating_add(1);
                if state.pending_class_body {
                    state.class_body_depths.push(state.brace_depth);
                    state.pending_class_body = false;
                }
            }
            b'}' => {
                if state.class_body_depths.last() == Some(&state.brace_depth) {
                    state.class_body_depths.pop();
                }
                state.brace_depth = state.brace_depth.saturating_sub(1);
            }
            _ => {}
        }
        idx += 1;
    }

    (state, false)
}

fn dart_line_starts_type_declaration(line: &str) -> bool {
    let trimmed = line.trim_start();
    trimmed.starts_with("class ")
        || trimmed.starts_with("abstract class ")
        || trimmed.starts_with("base class ")
        || trimmed.starts_with("final class ")
        || trimmed.starts_with("interface class ")
        || trimmed.starts_with("enum ")
        || trimmed.starts_with("mixin ")
        || trimmed.starts_with("extension ")
}

fn empty_prefix_semicolon_declaration_in_class(
    line: &str,
    name_start: usize,
    state: DartScanState,
) -> bool {
    if !line[..name_start].trim().is_empty() {
        return false;
    }
    let Some(open_paren) = line[name_start..]
        .find('(')
        .map(|offset| name_start + offset)
    else {
        return false;
    };
    let after_paren = &line[open_paren + 1..];
    let after_args = after_paren
        .find(')')
        .and_then(|close| after_paren.get(close + 1..))
        .unwrap_or_default()
        .trim_start();
    if !after_args.starts_with(';') {
        return false;
    }
    let (state, in_line_comment) = dart_scan_line_until(line, name_start, state);
    !in_line_comment && state.in_class_member_scope()
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
