pub(super) fn collapse_whitespace(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

pub(super) fn extract_js_module_specifier(text: &str) -> Option<String> {
    if let Some((_, after_from)) = text.rsplit_once(" from ") {
        return extract_quoted_string(after_from);
    }
    let rest = text.strip_prefix("import ")?;
    extract_quoted_string(rest)
}

pub(super) fn extract_js_import_clause(text: &str) -> Option<&str> {
    let rest = text.strip_prefix("import ")?;
    let (clause, _) = rest.rsplit_once(" from ")?;
    Some(clause)
}

pub(super) fn extract_quoted_string(text: &str) -> Option<String> {
    let quote = text.find(['"', '\'', '`'])?;
    let quote_char = text[quote..].chars().next()?;
    let after_quote = &text[quote + quote_char.len_utf8()..];
    let mut escaped = false;
    let mut idx = 0;
    while idx < after_quote.len() {
        let ch = after_quote[idx..].chars().next()?;
        if escaped {
            escaped = false;
            idx += ch.len_utf8();
            continue;
        }
        if ch == '\\' {
            escaped = true;
            idx += ch.len_utf8();
            continue;
        }
        if quote_char == '`' && ch == '$' && after_quote[idx + ch.len_utf8()..].starts_with('{') {
            idx = skip_template_interpolation(after_quote, idx + ch.len_utf8() + 1)?;
            continue;
        }
        if ch == quote_char {
            return Some(after_quote[..idx].to_string());
        }
        idx += ch.len_utf8();
    }
    None
}

fn skip_template_interpolation(text: &str, mut idx: usize) -> Option<usize> {
    let mut brace_depth = 1usize;
    let mut in_single = false;
    let mut in_double = false;
    let mut in_backtick = false;
    let mut escaped = false;

    while idx < text.len() {
        let ch = text[idx..].chars().next()?;
        if escaped {
            escaped = false;
            idx += ch.len_utf8();
            continue;
        }
        if (in_single || in_double || in_backtick) && ch == '\\' {
            escaped = true;
            idx += ch.len_utf8();
            continue;
        }
        match ch {
            '\'' if !in_double && !in_backtick => in_single = !in_single,
            '"' if !in_single && !in_backtick => in_double = !in_double,
            '`' if !in_single && !in_double => in_backtick = !in_backtick,
            '{' if !in_single && !in_double && !in_backtick => brace_depth += 1,
            '}' if !in_single && !in_double && !in_backtick => {
                brace_depth -= 1;
                idx += ch.len_utf8();
                if brace_depth == 0 {
                    return Some(idx);
                }
                continue;
            }
            _ => {}
        }
        idx += ch.len_utf8();
    }
    None
}

pub(super) fn go_default_package_alias(module: &str) -> String {
    let module = module.trim_end_matches('/');
    let last_segment = module.rsplit('/').next().unwrap_or(module);
    let without_version = last_segment
        .rsplit_once(".v")
        .filter(|(_, version)| !version.is_empty() && version.chars().all(|ch| ch.is_ascii_digit()))
        .map(|(name, _)| name)
        .unwrap_or(last_segment);
    without_version.replace('-', "_")
}

pub(super) fn split_alias(text: &str) -> (&str, Option<&str>) {
    if let Some((name, alias)) = text.split_once(" as ") {
        (name.trim(), Some(alias.trim()))
    } else {
        (text.trim(), None)
    }
}

pub(super) fn split_rust_use_group(text: &str) -> Option<(&str, &str)> {
    let mut depth = 0usize;
    let mut start = None;

    for (idx, ch) in text.char_indices() {
        match ch {
            '{' => {
                if depth == 0 {
                    start = Some(idx);
                }
                depth += 1;
            }
            '}' if depth > 0 => {
                depth -= 1;
                if depth == 0 {
                    let start = start?;
                    if text[idx + ch.len_utf8()..].trim().is_empty() {
                        return Some((text[..start].trim(), text[start + 1..idx].trim()));
                    }
                    return None;
                }
            }
            _ => {}
        }
    }

    None
}

pub(super) fn rust_join_use_path(prefix: &str, item: &str) -> Option<String> {
    let prefix = prefix.trim().trim_end_matches("::").trim();
    let item = item.trim();
    if item.is_empty() {
        return None;
    }

    let (item_path, alias) = split_alias(item);
    let item_path = item_path.trim();
    if item_path.is_empty() {
        return None;
    }

    let path = if item_path == "self" {
        if prefix.is_empty() {
            return None;
        }
        prefix.to_string()
    } else if prefix.is_empty() {
        item_path.to_string()
    } else {
        format!("{prefix}::{item_path}")
    };

    Some(match alias {
        Some(alias) if !alias.is_empty() => format!("{path} as {alias}"),
        _ => path,
    })
}

pub(super) fn split_top_level(text: &str, delimiter: char) -> Result<Vec<&str>, &'static str> {
    let mut parts = Vec::new();
    let mut start = 0;
    let mut paren_depth = 0usize;
    let mut brace_depth = 0usize;
    let mut bracket_depth = 0usize;
    let mut in_single = false;
    let mut in_double = false;
    let mut escaped = false;

    for (idx, ch) in text.char_indices() {
        if escaped {
            escaped = false;
            continue;
        }
        if (in_single || in_double) && ch == '\\' {
            escaped = true;
            continue;
        }
        match ch {
            '\'' if !in_double => in_single = !in_single,
            '"' if !in_single => in_double = !in_double,
            '(' if !in_single && !in_double => paren_depth += 1,
            ')' if !in_single && !in_double && paren_depth > 0 => paren_depth -= 1,
            ')' if !in_single && !in_double => return Err("unbalanced closing parenthesis"),
            '{' if !in_single && !in_double => brace_depth += 1,
            '}' if !in_single && !in_double && brace_depth > 0 => brace_depth -= 1,
            '}' if !in_single && !in_double => return Err("unbalanced closing brace"),
            '[' if !in_single && !in_double => bracket_depth += 1,
            ']' if !in_single && !in_double && bracket_depth > 0 => bracket_depth -= 1,
            ']' if !in_single && !in_double => return Err("unbalanced closing bracket"),
            ch if ch == delimiter
                && !in_single
                && !in_double
                && paren_depth == 0
                && brace_depth == 0
                && bracket_depth == 0 =>
            {
                parts.push(text[start..idx].trim());
                start = idx + ch.len_utf8();
            }
            _ => {}
        }
    }

    parts.push(text[start..].trim());

    if in_single || in_double {
        return Err("unterminated string literal");
    }
    if paren_depth != 0 || brace_depth != 0 || bracket_depth != 0 {
        return Err("unbalanced opening delimiter");
    }

    Ok(parts)
}

pub(super) fn is_ruby_constant_name(name: &str) -> bool {
    is_uppercase_ascii_alnum_underscore_name(name)
}

fn is_uppercase_ascii_alnum_underscore_name(name: &str) -> bool {
    name.chars()
        .next()
        .is_some_and(|ch| ch.is_ascii_uppercase())
        && name
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || ch == '_')
}

pub(super) fn dart_import_alias(text: &str) -> Option<String> {
    let after_as = text.split_once(" as ")?.1;
    let alias = after_as
        .split_whitespace()
        .next()
        .unwrap_or_default()
        .trim_end_matches(';');
    if alias.is_empty() {
        None
    } else {
        Some(alias.to_string())
    }
}

pub(super) fn is_elixir_alias(name: &str) -> bool {
    is_uppercase_ascii_alnum_underscore_name(name)
}

pub(super) fn is_elixir_alias_path(path: &str) -> bool {
    path.split('.').all(is_elixir_alias)
}

pub(super) fn elixir_alias_as(text: &str) -> Option<String> {
    let after = text.split_once(" as: ")?.1;
    let alias = after
        .split([',', ' ', ')', ']'])
        .next()
        .unwrap_or_default()
        .trim();
    if is_elixir_alias(alias) {
        Some(alias.to_string())
    } else {
        None
    }
}
