use crate::models::ImportRelation;

use super::super::context::{ExtractedImports, ImportResolutionContext, LocalCallBinding};
use super::super::helpers::{split_alias, split_rust_use_group, split_top_level};

pub(crate) fn parse_scala_import_statement(
    text: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    let normalized = text.trim().trim_end_matches(';').trim();
    let Some(rest) = normalized.strip_prefix("import ") else {
        return;
    };

    let Ok(items) = split_top_level(rest, ',') else {
        return;
    };
    for item in items {
        register_scala_import_or_group(item, rel_path, import_context, extracted);
    }
}

fn register_scala_import_or_group(
    item: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    let item = item.trim();
    if item.is_empty() {
        return;
    }

    if let Some((base, group)) = split_scala_import_group(item) {
        let Ok(selectors) = split_top_level(group, ',') else {
            return;
        };
        for selector in selectors {
            if let Some(joined) = scala_join_import_path(base, selector) {
                register_scala_import_or_group(&joined, rel_path, import_context, extracted);
            }
        }
        return;
    }

    if let Some(module) = scala_wildcard_module(item) {
        extracted.imports.push(ImportRelation {
            file_path: rel_path.to_string(),
            module_name: module,
        });
        return;
    }

    register_scala_import_item(item, rel_path, import_context, extracted);
}

fn register_scala_import_item(
    item: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    let (target, alias) = split_scala_alias(item);
    let target = target.trim();
    if target.is_empty() {
        return;
    }

    extracted.imports.push(ImportRelation {
        file_path: rel_path.to_string(),
        module_name: target.to_string(),
    });

    let Some((package, simple_name)) = target.rsplit_once('.') else {
        return;
    };
    let alias = alias.unwrap_or(simple_name).trim();
    if alias.is_empty() || alias == "_" || alias == "*" {
        return;
    }

    let candidate_files = import_context.scala_package_files(package);
    if candidate_files.is_empty() {
        extracted
            .bindings
            .member
            .insert(alias.to_string(), target.to_string());
        return;
    }

    extracted.bindings.bare.remove(alias);
    extracted.bindings.member.remove(alias);
    extracted
        .bindings
        .local_member
        .insert(alias.to_string(), candidate_files.clone());
    extracted.bindings.local_bare.insert(
        alias.to_string(),
        LocalCallBinding::named(candidate_files, simple_name.to_string()),
    );
}

fn split_scala_import_group(text: &str) -> Option<(&str, &str)> {
    let (base, group) = split_rust_use_group(text)?;
    let base = base.trim().trim_end_matches('.').trim();
    if base.is_empty() || group.is_empty() {
        return None;
    }
    Some((base, group))
}

fn scala_join_import_path(prefix: &str, item: &str) -> Option<String> {
    let prefix = prefix.trim().trim_end_matches('.').trim();
    let (item_path, alias) = split_scala_alias(item);
    let item_path = item_path.trim();
    if item_path.is_empty() {
        return None;
    }

    let path = if prefix.is_empty() {
        item_path.to_string()
    } else {
        format!("{prefix}.{item_path}")
    };
    Some(match alias {
        Some(alias) if !alias.trim().is_empty() => format!("{path} as {}", alias.trim()),
        _ => path,
    })
}

fn scala_wildcard_module(item: &str) -> Option<String> {
    let (target, _) = split_scala_alias(item);
    let wildcard = target
        .split('.')
        .position(|segment| matches!(segment.trim(), "_" | "*"))?;
    let module = target
        .split('.')
        .take(wildcard)
        .filter(|segment| !segment.trim().is_empty())
        .collect::<Vec<_>>()
        .join(".");
    (!module.is_empty()).then_some(module)
}

fn split_scala_alias(text: &str) -> (&str, Option<&str>) {
    let (target, alias) = split_alias(text);
    if alias.is_some() {
        return (target, alias);
    }
    text.split_once("=>")
        .map(|(target, alias)| (target.trim(), Some(alias.trim())))
        .unwrap_or((text.trim(), None))
}
