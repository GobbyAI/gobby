use crate::models::ImportRelation;

use super::super::context::{ExternalImportBinding, ExtractedImports, ImportResolutionContext};
use super::super::helpers::{
    collapse_whitespace, extract_js_import_clause, extract_js_module_specifier, split_alias,
    split_top_level,
};
use super::super::predicates::{is_external_js_module, is_external_python_module};

pub(crate) fn parse_python_import_statement(
    text: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    if let Some(rest) = text.strip_prefix("import ") {
        let Ok(entries) = split_top_level(rest, ',') else {
            return;
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
        return;
    }

    let Some(rest) = text.strip_prefix("from ") else {
        extracted.imports.push(ImportRelation {
            file_path: rel_path.to_string(),
            module_name: text.to_string(),
        });
        return;
    };
    let Some((module, imported)) = rest.split_once(" import ") else {
        extracted.imports.push(ImportRelation {
            file_path: rel_path.to_string(),
            module_name: text.to_string(),
        });
        return;
    };

    let module = module.trim();
    if module.is_empty() {
        return;
    }
    extracted.imports.push(ImportRelation {
        file_path: rel_path.to_string(),
        module_name: module.to_string(),
    });

    if !is_external_python_module(module, import_context) {
        return;
    }

    let imported = imported.trim().trim_matches(|ch| matches!(ch, '(' | ')'));
    let Ok(entries) = split_top_level(imported, ',') else {
        return;
    };
    for entry in entries {
        let entry = entry.trim();
        if entry.is_empty() || entry == "*" {
            continue;
        }
        let (imported_name, alias) = split_alias(entry);
        let local_alias = alias.unwrap_or(imported_name).to_string();
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
}

pub(crate) fn parse_js_import_statement(
    text: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    let normalized = collapse_whitespace(text);
    let Some(specifier) = extract_js_module_specifier(&normalized) else {
        extracted.imports.push(ImportRelation {
            file_path: rel_path.to_string(),
            module_name: normalized,
        });
        return;
    };

    extracted.imports.push(ImportRelation {
        file_path: rel_path.to_string(),
        module_name: specifier.clone(),
    });

    if !is_external_js_module(&specifier, import_context) {
        return;
    }

    let Some(clause) = extract_js_import_clause(&normalized) else {
        return;
    };
    let clause = clause.trim();
    if clause.is_empty() || (clause.starts_with("type ") && !clause.contains(',')) {
        return;
    }

    let Ok(parts) = split_top_level(clause, ',') else {
        return;
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
                return;
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

        let alias = part.strip_prefix("type ").unwrap_or(part).trim();
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
}
