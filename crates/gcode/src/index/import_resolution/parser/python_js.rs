use crate::models::ImportRelation;

use super::super::context::{ExternalImportBinding, ExtractedImports, ImportResolutionContext};
use super::super::helpers::{
    collapse_whitespace, extract_js_import_clause, extract_js_module_specifier, split_alias,
    split_top_level,
};
use super::super::predicates::{is_external_js_module, is_external_python_module};
use super::push_unparsed_import;

pub(crate) fn parse_python_import_statement(
    text: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) -> anyhow::Result<()> {
    if let Some(rest) = text.strip_prefix("import ") {
        let Ok(entries) = split_top_level(rest, ',') else {
            return Ok(());
        };
        for entry in entries {
            let entry = entry.trim();
            if entry.is_empty() {
                continue;
            }

            let (module, alias) = split_alias(entry);
            if module.is_empty() || module.chars().any(char::is_whitespace) {
                continue;
            }
            extracted.imports.push(ImportRelation {
                file_path: rel_path.to_string(),
                module_name: module.to_string(),
            });

            if is_external_python_module(module, import_context) {
                let local_alias = alias
                    .map(ToOwned::to_owned)
                    .unwrap_or_else(|| module.split('.').next().unwrap_or(module).to_string());
                extracted
                    .bindings
                    .member
                    .insert(local_alias, module.to_string());
            }
        }
        return Ok(());
    }

    let Some(rest) = text.strip_prefix("from ") else {
        push_unparsed_import(rel_path, text, extracted)?;
        return Ok(());
    };
    let Some((module, imported)) = rest.split_once(" import ") else {
        push_unparsed_import(rel_path, text, extracted)?;
        return Ok(());
    };

    let module = module.trim();
    if module.is_empty() {
        return Ok(());
    }
    extracted.imports.push(ImportRelation {
        file_path: rel_path.to_string(),
        module_name: module.to_string(),
    });

    if !is_external_python_module(module, import_context) {
        let Some(local_module) = python_local_module_lookup(module, rel_path) else {
            return Ok(());
        };
        let imported = imported.trim().trim_matches(|ch| matches!(ch, '(' | ')'));
        let Ok(entries) = split_top_level(imported, ',') else {
            return Ok(());
        };
        for entry in entries {
            let entry = entry.trim();
            if entry.is_empty() || entry == "*" {
                continue;
            }
            let (imported_name, alias) = split_alias(entry);
            let Some(binding) = import_context.python_local_symbol(&local_module, imported_name)
            else {
                continue;
            };
            let local_alias = alias.unwrap_or(imported_name).to_string();
            extracted.bindings.bare.remove(&local_alias);
            extracted
                .bindings
                .local_bare
                .insert(local_alias, binding.clone());
        }
        return Ok(());
    }

    let imported = imported.trim().trim_matches(|ch| matches!(ch, '(' | ')'));
    let Ok(entries) = split_top_level(imported, ',') else {
        return Ok(());
    };
    for entry in entries {
        let entry = entry.trim();
        if entry.is_empty() || entry == "*" {
            // Wildcard imports are valid import edges, but they do not name
            // stable local call bindings. External bare-call resolution handles
            // wildcard modules only when a parser records unambiguous state.
            continue;
        }
        let (imported_name, alias) = split_alias(entry);
        let local_alias = alias.unwrap_or(imported_name).to_string();
        extracted.bindings.local_bare.remove(&local_alias);
        extracted.bindings.bare.insert(
            local_alias.clone(),
            ExternalImportBinding {
                module: module.to_string(),
                callee_name: imported_name.to_string(),
            },
        );
        extracted
            .bindings
            .member
            .insert(local_alias, module.to_string());
    }
    Ok(())
}

fn python_local_module_lookup(module: &str, rel_path: &str) -> Option<String> {
    if !module.starts_with('.') {
        return Some(module.to_string());
    }

    let level = module.chars().take_while(|ch| *ch == '.').count();
    let suffix = module[level..].trim_matches('.');
    let mut parts = rel_path
        .trim_end_matches(".pyi")
        .trim_end_matches(".py")
        .split('/')
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>();
    if parts
        .last()
        .is_some_and(|file_name| *file_name != "__init__")
    {
        parts.pop();
    }
    for _ in 1..level {
        parts.pop()?;
    }
    if !suffix.is_empty() {
        parts.extend(suffix.split('.').filter(|part| !part.is_empty()));
    }
    if parts.is_empty() {
        None
    } else {
        Some(parts.join("."))
    }
}

pub(crate) fn parse_js_import_statement(
    text: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) -> anyhow::Result<()> {
    let normalized = collapse_whitespace(text);
    let Some(specifier) = extract_js_module_specifier(&normalized) else {
        push_unparsed_import(rel_path, &normalized, extracted)?;
        return Ok(());
    };

    extracted.imports.push(ImportRelation {
        file_path: rel_path.to_string(),
        module_name: specifier.clone(),
    });

    let Some(clause) = extract_js_import_clause(&normalized) else {
        return Ok(());
    };
    let clause = clause.trim();
    if clause.is_empty() || clause.starts_with("type ") {
        return Ok(());
    }

    if !is_external_js_module(&specifier, import_context) {
        parse_js_local_import_clause(&specifier, clause, rel_path, import_context, extracted)?;
        return Ok(());
    }

    let Ok(parts) = split_top_level(clause, ',') else {
        return Ok(());
    };
    for part in parts {
        let part = part.trim();
        if part.is_empty() {
            continue;
        }
        if let Some(alias) = part.strip_prefix("* as ") {
            let alias = alias.trim();
            if !alias.is_empty() {
                extracted
                    .bindings
                    .member
                    .insert(alias.to_string(), specifier.clone());
            }
            continue;
        }
        if part.starts_with('{') && part.ends_with('}') {
            let inner = &part[1..part.len() - 1];
            let Ok(items) = split_top_level(inner, ',') else {
                return Ok(());
            };
            for item in items {
                let item = item.trim();
                if item.is_empty() || item.starts_with("type ") {
                    continue;
                }
                let (imported_name, alias) = split_alias(item);
                let local_alias = alias.unwrap_or(imported_name).to_string();
                extracted.bindings.bare.insert(
                    local_alias.clone(),
                    ExternalImportBinding {
                        module: specifier.clone(),
                        callee_name: imported_name.to_string(),
                    },
                );
                extracted
                    .bindings
                    .member
                    .insert(local_alias, specifier.clone());
            }
            continue;
        }

        if part.starts_with("type ") {
            continue;
        }
        let alias = part.trim();
        if alias.is_empty() {
            continue;
        }
        extracted.bindings.bare.insert(
            alias.to_string(),
            ExternalImportBinding {
                module: specifier.clone(),
                callee_name: "default".to_string(),
            },
        );
        extracted
            .bindings
            .member
            .insert(alias.to_string(), specifier.clone());
    }
    Ok(())
}

fn parse_js_local_import_clause(
    specifier: &str,
    clause: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) -> anyhow::Result<()> {
    let Some(module) = import_context.js_local_module(rel_path, specifier) else {
        return Ok(());
    };

    let Ok(parts) = split_top_level(clause, ',') else {
        return Ok(());
    };
    for part in parts {
        let part = part.trim();
        if part.is_empty() {
            continue;
        }
        if let Some(alias) = part.strip_prefix("* as ") {
            let alias = alias.trim();
            if !alias.is_empty() {
                extracted.bindings.member.remove(alias);
                extracted
                    .bindings
                    .local_member
                    .insert(alias.to_string(), module.exports.clone());
            }
            continue;
        }
        if part.starts_with('{') && part.ends_with('}') {
            let inner = &part[1..part.len() - 1];
            let Ok(items) = split_top_level(inner, ',') else {
                return Ok(());
            };
            for item in items {
                let item = item.trim();
                if item.is_empty() || item.starts_with("type ") {
                    continue;
                }
                let (imported_name, alias) = split_alias(item);
                if let Some(binding) = module.exports.get(imported_name) {
                    let local_alias = alias.unwrap_or(imported_name).to_string();
                    extracted.bindings.bare.remove(&local_alias);
                    extracted.bindings.member.remove(&local_alias);
                    extracted
                        .bindings
                        .local_bare
                        .insert(local_alias, binding.clone());
                }
            }
            continue;
        }

        if part.starts_with("type ") {
            continue;
        }
        let alias = part.trim();
        if alias.is_empty() {
            continue;
        }
        if let Some(binding) = module.exports.get("default") {
            extracted.bindings.bare.remove(alias);
            extracted.bindings.member.remove(alias);
            extracted
                .bindings
                .local_bare
                .insert(alias.to_string(), binding.clone());
        }
    }
    Ok(())
}
