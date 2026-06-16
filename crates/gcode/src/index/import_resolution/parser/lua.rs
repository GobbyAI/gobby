use crate::models::ImportRelation;

use super::super::context::{ExtractedImports, ImportResolutionContext, LocalCallBinding};
use super::super::helpers::{collapse_whitespace, extract_quoted_string};

pub(crate) fn parse_lua_import_statement(
    text: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    let normalized = collapse_whitespace(text);
    if !normalized.contains("require") {
        return;
    }
    let Some(module) = extract_quoted_string(&normalized) else {
        return;
    };

    extracted.imports.push(ImportRelation {
        file_path: rel_path.to_string(),
        module_name: module.clone(),
    });

    let candidate_files = import_context.lua_module_files(&module);
    if candidate_files.is_empty() {
        return;
    }

    let Some((alias, member)) = lua_require_assignment(&normalized) else {
        return;
    };
    if let Some(member) = member {
        extracted.bindings.local_bare.insert(
            alias,
            LocalCallBinding {
                candidate_files,
                callee_name: member,
            },
        );
    } else {
        extracted
            .bindings
            .local_member
            .insert(alias, candidate_files);
    }
}

pub(crate) fn resolve_lua_require_member_callee(
    import_context: &ImportResolutionContext,
    callee_name: &str,
    qualifier_path: Option<&str>,
    is_member_call: bool,
) -> Option<LocalCallBinding> {
    if !is_member_call {
        return None;
    }
    let qualifier_path = qualifier_path?.trim();
    if !qualifier_path.starts_with("require") {
        return None;
    }
    let module = extract_quoted_string(qualifier_path)?;
    let candidate_files = import_context.lua_module_files(&module);
    if candidate_files.is_empty() {
        return None;
    }
    Some(LocalCallBinding {
        candidate_files,
        callee_name: callee_name.to_string(),
    })
}

fn lua_require_assignment(text: &str) -> Option<(String, Option<String>)> {
    let (lhs, rhs) = text.split_once('=')?;
    let lhs = lhs
        .trim()
        .strip_prefix("local ")
        .unwrap_or(lhs.trim())
        .trim();
    if lhs.contains(',') || !is_lua_identifier(lhs) {
        return None;
    }

    let require_pos = rhs.find("require")?;
    let after_require = &rhs[require_pos + "require".len()..];
    let after_literal = after_first_quoted_string(after_require)?;
    Some((lhs.to_string(), lua_member_after_require(after_literal)))
}

fn after_first_quoted_string(text: &str) -> Option<&str> {
    let quote = text.find(['"', '\''])?;
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
        if ch == quote_char {
            return Some(&after_quote[idx + ch.len_utf8()..]);
        }
        idx += ch.len_utf8();
    }
    None
}

fn lua_member_after_require(text: &str) -> Option<String> {
    let mut rest = text.trim_start();
    if let Some(after_paren) = rest.strip_prefix(')') {
        rest = after_paren.trim_start();
    }
    let rest = rest.strip_prefix(['.', ':'])?;
    let member = rest
        .chars()
        .take_while(|ch| ch.is_ascii_alphanumeric() || *ch == '_')
        .collect::<String>();
    if is_lua_identifier(&member) {
        Some(member)
    } else {
        None
    }
}

fn is_lua_identifier(name: &str) -> bool {
    let mut chars = name.chars();
    let Some(first) = chars.next() else {
        return false;
    };
    (first.is_ascii_alphabetic() || first == '_')
        && chars.all(|ch| ch.is_ascii_alphanumeric() || ch == '_')
}
