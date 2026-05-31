use crate::index::semantic::SemanticCallResolver;
use crate::models::CallRelation;

use super::resolution::CallSyntaxKind;
use super::text::{
    is_identifier_continue, is_textual_call_name_byte, line_terminator_len, should_ignore_call_name,
};
use super::{CallExtractionContext, CallSite, materialize_call};

pub(super) fn extract_textual_dart_calls(
    source: &[u8],
    ctx: CallExtractionContext<'_>,
    mut semantic_resolver: Option<&mut (dyn SemanticCallResolver + '_)>,
) -> anyhow::Result<Vec<CallRelation>> {
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

        for candidate in textual_call_candidates(line, line_start_byte, row + 1, &['.']) {
            let candidate_line_byte = candidate.name_byte.saturating_sub(line_start_byte);
            if dart_textual_candidate_in_ignored_context(line, candidate_line_byte, dart_state) {
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

        dart_state = dart_state_after_line(line, dart_state);
        line_start_byte += line.len() + terminator_len;
    }

    Ok(calls)
}

fn textual_call_candidates(
    line: &str,
    line_start_byte: usize,
    line_number: usize,
    separators: &[char],
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
