use crate::models::ImportRelation;

use super::super::context::{
    ExternalImportBinding, ExternalRootBinding, ExtractedImports, ImportResolutionContext,
};
use super::super::helpers::{
    extract_quoted_string, go_default_package_alias, rust_join_use_path, split_alias,
    split_rust_use_group, split_top_level,
};
use super::super::predicates::{is_external_go_module, is_external_rust_root};

pub(crate) fn parse_go_import_statement(
    text: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) -> anyhow::Result<()> {
    let trimmed = text.trim();
    let Some(rest) = trimmed.strip_prefix("import") else {
        anyhow::bail!("expected Go import statement, got `{trimmed}`");
    };
    if rest
        .chars()
        .next()
        .is_some_and(|ch| !ch.is_whitespace() && ch != '(')
    {
        anyhow::bail!("expected Go import statement, got `{trimmed}`");
    }

    let rest = rest.trim();
    if rest.starts_with('(') {
        let block = rest.trim_start_matches('(').trim_end_matches(')');
        for line in block.lines() {
            parse_go_import_spec(line.trim(), rel_path, import_context, extracted);
        }
    } else {
        parse_go_import_spec(rest, rel_path, import_context, extracted);
    }
    Ok(())
}

fn parse_go_import_spec(
    text: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    let text = text.split("//").next().unwrap_or(text).trim();
    if text.is_empty() {
        return;
    }
    let Some(module) = extract_quoted_string(text) else {
        return;
    };

    extracted.imports.push(ImportRelation {
        file_path: rel_path.to_string(),
        module_name: module.clone(),
    });

    if !is_external_go_module(&module, import_context) {
        return;
    }

    let alias = text[..text.find(['"', '`']).unwrap_or(0)].trim();
    if matches!(alias, "_" | ".") {
        return;
    }
    let local_alias = if alias.is_empty() {
        go_default_package_alias(&module)
    } else {
        alias.to_string()
    };
    if !local_alias.is_empty() {
        extracted.bindings.member.insert(local_alias, module);
    }
}

pub(crate) fn parse_rust_import_statement(
    text: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    let trimmed = text.trim();
    let Some(rest) = trimmed.strip_prefix("use ") else {
        debug_assert!(
            trimmed.strip_prefix("use ").is_some(),
            "expected Rust use statement, got `{}`",
            trimmed
        );
        return;
    };
    let rest = rest.trim().trim_end_matches(';').trim();
    extracted.imports.push(ImportRelation {
        file_path: rel_path.to_string(),
        module_name: rest.to_string(),
    });

    if let Some((prefix, group)) = split_rust_use_group(rest) {
        register_rust_group_imports(prefix, group, import_context, extracted);
        return;
    }

    if rest.contains('*') {
        // Glob imports are intentionally not expanded because exported names are unknown here.
        return;
    }

    register_rust_path_import(rest, import_context, extracted);
}

fn register_rust_group_imports(
    prefix: &str,
    group: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    let Ok(items) = split_top_level(group, ',') else {
        return;
    };
    for item in items {
        if item.is_empty() {
            continue;
        }
        if let Some((nested_prefix, nested_group)) = split_rust_use_group(item) {
            let Some(full_prefix) = rust_join_use_path(prefix, nested_prefix) else {
                continue;
            };
            register_rust_group_imports(&full_prefix, nested_group, import_context, extracted);
            continue;
        }
        if item.contains('*') {
            // Glob imports are intentionally not expanded because exported names are unknown here.
            continue;
        }
        let Some(path) = rust_join_use_path(prefix, item) else {
            continue;
        };
        register_rust_path_import(&path, import_context, extracted);
    }
}

fn register_rust_path_import(
    path_and_alias: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    let normalized = path_and_alias.trim();
    if normalized.is_empty() {
        return;
    }
    let (path, alias) = split_alias(normalized);
    let segments: Vec<&str> = path.split("::").filter(|part| !part.is_empty()).collect();
    let Some(root) = segments.first().copied() else {
        return;
    };
    if !is_external_rust_root(root, import_context) {
        return;
    }

    extracted.bindings.external_roots.insert(
        root.to_string(),
        ExternalRootBinding {
            module: root.to_string(),
            module_from_qualifier: true,
        },
    );

    let Some(imported_name) = segments.last().copied() else {
        return;
    };
    let local_alias = alias.unwrap_or(imported_name);
    if local_alias.is_empty() {
        return;
    }

    let module = if segments.len() > 1 {
        segments[..segments.len() - 1].join("::")
    } else {
        root.to_string()
    };
    extracted.bindings.bare.insert(
        local_alias.to_string(),
        ExternalImportBinding {
            module: module.clone(),
            callee_name: imported_name.to_string(),
        },
    );
    extracted
        .bindings
        .member
        .insert(local_alias.to_string(), path.to_string());
}

#[cfg(test)]
mod tests {
    use std::panic::{AssertUnwindSafe, catch_unwind};

    use super::*;

    #[test]
    fn non_import_go_statement_does_not_record_raw_import() {
        let mut extracted = ExtractedImports::default();
        let result = parse_go_import_statement(
            "package main",
            "main.go",
            &ImportResolutionContext::default(),
            &mut extracted,
        );

        assert!(result.is_err());
        assert!(extracted.imports.is_empty());
    }

    #[test]
    fn non_use_rust_statement_does_not_record_raw_import() {
        let mut extracted = ExtractedImports::default();
        let result = catch_unwind(AssertUnwindSafe(|| {
            parse_rust_import_statement(
                "mod tests;",
                "lib.rs",
                &ImportResolutionContext::default(),
                &mut extracted,
            );
        }));

        if cfg!(debug_assertions) {
            assert!(result.is_err());
        } else {
            assert!(result.is_ok());
        }
        assert!(extracted.imports.is_empty());
    }
}
