use crate::models::ImportRelation;

use super::super::context::{ExternalImportBinding, ExtractedImports, ImportResolutionContext};
use super::super::helpers::{split_alias, split_rust_use_group, split_top_level};
use super::super::predicates::{is_external_java_class, is_external_php_symbol};

pub(crate) fn php_local_symbol_exists(
    import_context: &ImportResolutionContext,
    symbol: &str,
) -> bool {
    import_context.php_local_symbols.contains(symbol)
        || import_context
            .php_local_symbols
            .contains(&symbol.to_ascii_lowercase())
}

pub(crate) fn parse_php_import_statement(
    text: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    let normalized = text.trim().trim_end_matches(';').trim();
    let Some(rest) = normalized.strip_prefix("use ") else {
        return;
    };
    let (kind, rest) = if let Some(target) = rest.strip_prefix("function ") {
        (PhpImportKind::Function, target.trim())
    } else if let Some(target) = rest.strip_prefix("const ") {
        (PhpImportKind::Const, target.trim())
    } else {
        (PhpImportKind::ClassLike, rest.trim())
    };

    if let Some((base, group)) = split_php_use_group(rest) {
        let Ok(items) = split_top_level(group, ',') else {
            return;
        };
        for item in items {
            if let Some(target) = php_join_use_path(base, item) {
                register_php_import_or_wildcard(&target, kind, rel_path, import_context, extracted);
            }
        }
        return;
    }

    if rest.contains('{') || rest.contains('}') {
        extracted.imports.push(ImportRelation {
            file_path: rel_path.to_string(),
            module_name: rest.to_string(),
        });
        return;
    }

    let Ok(items) = split_top_level(rest, ',') else {
        return;
    };
    for item in items {
        register_php_import_or_wildcard(item, kind, rel_path, import_context, extracted);
    }
}

#[derive(Clone, Copy)]
enum PhpImportKind {
    ClassLike,
    Function,
    Const,
}

pub(crate) fn parse_kotlin_import_statement(
    text: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    let normalized = text.trim();
    let Some(rest) = normalized.strip_prefix("import ") else {
        return;
    };

    let target = rest
        .split_once(" as ")
        .map(|(target, _)| target.trim())
        .unwrap_or_else(|| rest.trim())
        .trim_end_matches(';')
        .trim();

    extracted.imports.push(ImportRelation {
        file_path: rel_path.to_string(),
        module_name: target.to_string(),
    });

    if target.ends_with(".*") || !is_external_java_class(target, import_context) {
        return;
    }

    let class_alias = rest
        .split_once(" as ")
        .map(|(_, alias)| alias.trim())
        .filter(|alias| !alias.is_empty())
        .unwrap_or_else(|| target.rsplit('.').next().unwrap_or(target));
    extracted
        .bindings
        .member
        .insert(class_alias.to_string(), target.to_string());
}

fn register_php_import_item(
    item: &str,
    kind: PhpImportKind,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    let item = item.trim();
    if item.is_empty() {
        return;
    }
    let (target, alias) = split_alias(item);
    let target = target.trim_start_matches('\\');
    extracted.imports.push(ImportRelation {
        file_path: rel_path.to_string(),
        module_name: target.to_string(),
    });
    if !is_external_php_symbol(target, import_context) {
        return;
    }

    let imported_name = target.rsplit('\\').next().unwrap_or(target);
    let local_alias = alias.unwrap_or(imported_name);
    if matches!(kind, PhpImportKind::Function) {
        let module = target
            .rsplit_once('\\')
            .map(|(module, _)| module)
            .unwrap_or(target);
        extracted.bindings.bare.insert(
            local_alias.to_string(),
            ExternalImportBinding {
                module: module.to_string(),
                callee_name: imported_name.to_string(),
            },
        );
    } else {
        extracted
            .bindings
            .member
            .insert(local_alias.to_string(), target.to_string());
    }
}

fn register_php_import_or_wildcard(
    item: &str,
    kind: PhpImportKind,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    if let Some(module) = php_wildcard_module(item) {
        extracted.imports.push(ImportRelation {
            file_path: rel_path.to_string(),
            module_name: module.clone(),
        });
        if is_external_php_symbol(&module, import_context) {
            extracted.bindings.bare_wildcard_modules.push(module);
        }
        return;
    }

    register_php_import_item(item, kind, rel_path, import_context, extracted);
}

fn php_wildcard_module(item: &str) -> Option<String> {
    let (target, _) = split_alias(item);
    let target = target.trim().trim_start_matches('\\');
    let wildcard = target
        .split('\\')
        .position(|segment| segment.trim() == "*")?;
    let module = target
        .split('\\')
        .take(wildcard)
        .filter(|segment| !segment.trim().is_empty())
        .collect::<Vec<_>>()
        .join("\\");
    (!module.is_empty()).then_some(module)
}

fn split_php_use_group(text: &str) -> Option<(&str, &str)> {
    let (base, group) = split_rust_use_group(text)?;
    if base.is_empty() || group.is_empty() {
        return None;
    }
    Some((base, group))
}

fn php_join_use_path(prefix: &str, item: &str) -> Option<String> {
    let prefix = prefix.trim().trim_start_matches('\\');
    let (item_path, alias) = split_alias(item);
    let item_path = item_path.trim().trim_start_matches('\\');
    if item_path.is_empty() {
        return None;
    }

    let path = if prefix.is_empty() {
        item_path.to_string()
    } else if prefix.ends_with('\\') {
        format!("{prefix}{item_path}")
    } else {
        format!("{prefix}\\{item_path}")
    };

    Some(match alias {
        Some(alias) if !alias.is_empty() => format!("{path} as {alias}"),
        _ => path,
    })
}
