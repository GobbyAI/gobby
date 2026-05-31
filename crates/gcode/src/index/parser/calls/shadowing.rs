use crate::models::Symbol;

use super::resolution::CallSyntaxKind;
use super::text::{is_identifier_start, trim_identifier_token};

pub(super) fn external_call_is_shadowed(
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
    let mut in_single = false;
    let mut in_double = false;
    let mut escaped = false;
    let mut chars = line.char_indices().peekable();

    while let Some((idx, ch)) = chars.next() {
        if escaped {
            escaped = false;
            continue;
        }
        if (in_single || in_double) && ch == '\\' {
            escaped = true;
            continue;
        }
        if !in_single && !in_double && ch == '/' && chars.peek().is_some_and(|(_, ch)| *ch == '/') {
            break;
        }
        if !in_single && !in_double && ch == '#' {
            break;
        }
        match ch {
            '\'' if !in_double => {
                in_single = !in_single;
                continue;
            }
            '"' if !in_single => {
                in_double = !in_double;
                continue;
            }
            _ => {}
        }
        if in_single || in_double || ch != '=' {
            continue;
        }
        let previous = line[..idx].chars().next_back();
        let next = line[idx + 1..].chars().next();
        if matches!(
            previous,
            Some('=' | '!' | '<' | '>' | ':' | '+' | '-' | '*' | '/' | '%' | '&' | '|' | '^' | '?')
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

#[cfg(test)]
mod tests {
    use super::split_assignment;

    #[test]
    fn split_assignment_ignores_bitwise_compound_operators() {
        assert_eq!(split_assignment("flags &= READ"), None);
        assert_eq!(split_assignment("flags |= WRITE"), None);
        assert_eq!(split_assignment("flags ^= EXECUTE"), None);
        assert_eq!(split_assignment("value += 1"), None);
        assert_eq!(
            split_assignment("flags = READ | WRITE"),
            Some(("flags ", " READ | WRITE"))
        );
    }
}
