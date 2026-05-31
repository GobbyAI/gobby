use crate::models::{ImportRelation, Symbol};

use super::context::{
    ExternalCallTarget, ExternalImportBinding, ExternalRootBinding, ExtractedImports,
    ImportBindings, ImportResolutionContext,
};
use super::helpers::{
    collapse_whitespace, dart_import_alias, elixir_alias_as, extract_js_import_clause,
    extract_js_module_specifier, extract_quoted_string, go_default_package_alias,
    is_elixir_alias_path, rust_join_use_path, split_alias, split_rust_use_group, split_top_level,
};
use super::predicates::{
    is_external_csharp_path, is_external_dart_uri, is_external_go_module, is_external_java_class,
    is_external_js_module, is_external_php_symbol, is_external_python_module,
    is_external_rust_root, rust_external_roots,
};

pub(crate) fn parse_import_statement(
    language: &str,
    text: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    match language {
        "python" => parse_python_import_statement(text, rel_path, import_context, extracted),
        "javascript" | "typescript" => {
            parse_js_import_statement(text, rel_path, import_context, extracted)
        }
        "go" => parse_go_import_statement(text, rel_path, import_context, extracted),
        "rust" => parse_rust_import_statement(text, rel_path, import_context, extracted),
        "java" => parse_java_import_statement(text, rel_path, import_context, extracted),
        "csharp" => parse_csharp_import_statement(text, rel_path, import_context, extracted),
        "php" => parse_php_import_statement(text, rel_path, import_context, extracted),
        "kotlin" => parse_kotlin_import_statement(text, rel_path, import_context, extracted),
        "swift" => parse_swift_import_statement(text, rel_path, extracted),
        "ruby" => parse_ruby_import_statement(text, rel_path, import_context, extracted),
        "dart" => parse_dart_import_statement(text, rel_path, import_context, extracted),
        "elixir" => parse_elixir_import_statement(text, rel_path, import_context, extracted),
        _ => extracted.imports.push(ImportRelation {
            file_path: rel_path.to_string(),
            module_name: text.to_string(),
        }),
    }
}

pub(crate) fn seed_import_bindings(
    language: &str,
    import_context: &ImportResolutionContext,
    bindings: &mut ImportBindings,
) {
    match language {
        "rust" => {
            for root in rust_external_roots(import_context) {
                bindings.external_roots.insert(
                    root.clone(),
                    ExternalRootBinding {
                        module: root,
                        module_from_qualifier: true,
                    },
                );
            }
        }
        "elixir" => {
            for (root, module) in &import_context.elixir_external_roots {
                if import_context.elixir_local_module_roots.contains(root) {
                    continue;
                }
                let module = import_context
                    .elixir_external_root_module(root)
                    .unwrap_or(module);
                bindings.external_roots.insert(
                    root.clone(),
                    ExternalRootBinding {
                        module: module.to_string(),
                        module_from_qualifier: true,
                    },
                );
            }
            for (root, module) in &import_context.elixir_external_root_overrides {
                if import_context.elixir_external_roots.contains_key(root)
                    || import_context.elixir_local_module_roots.contains(root)
                {
                    continue;
                }
                bindings.external_roots.insert(
                    root.clone(),
                    ExternalRootBinding {
                        module: module.clone(),
                        module_from_qualifier: true,
                    },
                );
            }
        }
        _ => {}
    }
}

pub(crate) fn resolve_external_callee(
    import_context: &ImportResolutionContext,
    import_bindings: &ImportBindings,
    symbols: &[Symbol],
    callee_name: &str,
    root_alias: Option<&str>,
    qualifier_path: Option<&str>,
    is_bare_call: bool,
) -> Option<ExternalCallTarget> {
    if is_bare_call {
        if symbols.iter().any(|symbol| symbol.name == callee_name) {
            return None;
        }
        if let Some(binding) = import_bindings.bare.get(callee_name) {
            return Some(ExternalCallTarget {
                module: binding.module.clone(),
                callee_name: binding.callee_name.clone(),
            });
        }
        if import_bindings.bare_wildcard_modules.len() == 1 {
            return Some(ExternalCallTarget {
                module: import_bindings.bare_wildcard_modules[0].clone(),
                callee_name: callee_name.to_string(),
            });
        }
        // Multiple wildcard imports make the source module ambiguous, so fail closed.
        return None;
    }

    let root_alias = root_alias?;
    if symbols.iter().any(|symbol| symbol.name == root_alias) {
        return None;
    }
    if let Some(module) = import_bindings.member.get(root_alias) {
        return Some(ExternalCallTarget {
            module: module.clone(),
            callee_name: callee_name.to_string(),
        });
    }

    let qualifier_path = qualifier_path?;
    if qualifier_path.starts_with('\\') {
        let module = qualifier_path.trim_start_matches('\\');
        if module.is_empty() {
            return None;
        }
        let local_symbol = format!("{module}\\{callee_name}");
        if php_local_symbol_exists(import_context, module)
            || php_local_symbol_exists(import_context, &local_symbol)
        {
            return None;
        }
        return Some(ExternalCallTarget {
            module: module.to_string(),
            callee_name: callee_name.to_string(),
        });
    }
    let (root_alias, qualifier_path) = csharp_global_qualifier_parts(root_alias, qualifier_path)
        .unwrap_or((root_alias, qualifier_path));
    let root_binding = import_bindings.external_roots.get(root_alias)?;
    let module = if root_binding.module_from_qualifier {
        qualifier_path.to_string()
    } else {
        root_binding.module.clone()
    };
    Some(ExternalCallTarget {
        module,
        callee_name: callee_name.to_string(),
    })
}
fn parse_python_import_statement(
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

fn parse_js_import_statement(
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

fn parse_go_import_statement(
    text: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    let Some(rest) = text.trim().strip_prefix("import") else {
        extracted.imports.push(ImportRelation {
            file_path: rel_path.to_string(),
            module_name: text.to_string(),
        });
        return;
    };

    let rest = rest.trim();
    if rest.starts_with('(') {
        let block = rest.trim_start_matches('(').trim_end_matches(')');
        for line in block.lines() {
            parse_go_import_spec(line.trim(), rel_path, import_context, extracted);
        }
    } else {
        parse_go_import_spec(rest, rel_path, import_context, extracted);
    }
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

fn parse_rust_import_statement(
    text: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    let Some(rest) = text.trim().strip_prefix("use ") else {
        extracted.imports.push(ImportRelation {
            file_path: rel_path.to_string(),
            module_name: text.to_string(),
        });
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

fn parse_java_import_statement(
    text: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    let normalized = text.trim().trim_end_matches(';').trim();
    let Some(rest) = normalized.strip_prefix("import ") else {
        extracted.imports.push(ImportRelation {
            file_path: rel_path.to_string(),
            module_name: normalized.to_string(),
        });
        return;
    };

    let (is_static, target) = rest
        .strip_prefix("static ")
        .map(|target| (true, target.trim()))
        .unwrap_or((false, rest.trim()));
    extracted.imports.push(ImportRelation {
        file_path: rel_path.to_string(),
        module_name: target.to_string(),
    });

    if target.ends_with(".*") {
        return;
    }

    if is_static {
        let Some((class_path, member_name)) = target.rsplit_once('.') else {
            return;
        };
        if !is_external_java_class(class_path, import_context) {
            return;
        }
        extracted.bindings.bare.insert(
            member_name.to_string(),
            ExternalImportBinding {
                module: class_path.to_string(),
                callee_name: member_name.to_string(),
            },
        );
        return;
    }

    if !is_external_java_class(target, import_context) {
        return;
    }
    let class_alias = target.rsplit('.').next().unwrap_or(target);
    extracted
        .bindings
        .member
        .insert(class_alias.to_string(), target.to_string());
}

fn parse_csharp_import_statement(
    text: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    let normalized = text.trim().trim_end_matches(';').trim();
    let Some(rest) = normalized.strip_prefix("using ") else {
        extracted.imports.push(ImportRelation {
            file_path: rel_path.to_string(),
            module_name: normalized.to_string(),
        });
        return;
    };

    if let Some(target) = rest.strip_prefix("static ") {
        let target = normalize_csharp_global_alias(target.trim());
        extracted.imports.push(ImportRelation {
            file_path: rel_path.to_string(),
            module_name: target.clone(),
        });
        if is_external_csharp_path(&target, import_context) {
            extracted.bindings.bare_wildcard_modules.push(target);
        }
        return;
    }

    if let Some((alias, target)) = rest.split_once('=') {
        let alias = alias.trim();
        let target = normalize_csharp_global_alias(target.trim());
        extracted.imports.push(ImportRelation {
            file_path: rel_path.to_string(),
            module_name: target.clone(),
        });
        if !alias.is_empty() && is_external_csharp_path(&target, import_context) {
            extracted.bindings.member.insert(alias.to_string(), target);
        }
        return;
    }

    let namespace = normalize_csharp_global_alias(rest.trim());
    extracted.imports.push(ImportRelation {
        file_path: rel_path.to_string(),
        module_name: namespace.clone(),
    });
    if !is_external_csharp_path(&namespace, import_context) {
        return;
    }
    if let Some(root) = namespace.split('.').next()
        && !root.is_empty()
    {
        extracted.bindings.external_roots.insert(
            root.to_string(),
            ExternalRootBinding {
                module: root.to_string(),
                module_from_qualifier: true,
            },
        );
    }
}

fn normalize_csharp_global_alias(path: &str) -> String {
    path.strip_prefix("global::").unwrap_or(path).to_string()
}

fn csharp_global_qualifier_parts<'a>(
    root_alias: &'a str,
    qualifier_path: &'a str,
) -> Option<(&'a str, &'a str)> {
    if root_alias != "global" {
        return None;
    }
    let qualifier_path = qualifier_path.strip_prefix("global::")?;
    let root_alias = qualifier_path
        .split('.')
        .next()
        .filter(|root| !root.is_empty())?;
    Some((root_alias, qualifier_path))
}

fn php_local_symbol_exists(import_context: &ImportResolutionContext, symbol: &str) -> bool {
    import_context.php_local_symbols.contains(symbol)
        || import_context
            .php_local_symbols
            .contains(&symbol.to_ascii_lowercase())
}

fn parse_php_import_statement(
    text: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    let normalized = text.trim().trim_end_matches(';').trim();
    let Some(rest) = normalized.strip_prefix("use ") else {
        extracted.imports.push(ImportRelation {
            file_path: rel_path.to_string(),
            module_name: normalized.to_string(),
        });
        return;
    };
    let (kind, rest) = if let Some(target) = rest.strip_prefix("function ") {
        (PhpImportKind::Function, target.trim())
    } else if let Some(target) = rest.strip_prefix("const ") {
        (PhpImportKind::Const, target.trim())
    } else {
        (PhpImportKind::ClassLike, rest.trim())
    };

    if split_top_level(rest, ',')
        .unwrap_or_default()
        .into_iter()
        .any(|item| item.trim() == "*")
    {
        extracted.imports.push(ImportRelation {
            file_path: rel_path.to_string(),
            module_name: rest.to_string(),
        });
        return;
    }

    if let Some((base, group)) = split_php_use_group(rest) {
        let Ok(items) = split_top_level(group, ',') else {
            return;
        };
        for item in items {
            if let Some(target) = php_join_use_path(base, item) {
                register_php_import_item(&target, kind, rel_path, import_context, extracted);
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
        register_php_import_item(item, kind, rel_path, import_context, extracted);
    }
}

#[derive(Clone, Copy)]
enum PhpImportKind {
    ClassLike,
    Function,
    Const,
}

fn parse_kotlin_import_statement(
    text: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    let normalized = text.trim();
    let Some(rest) = normalized.strip_prefix("import ") else {
        extracted.imports.push(ImportRelation {
            file_path: rel_path.to_string(),
            module_name: normalized.to_string(),
        });
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

fn parse_swift_import_statement(text: &str, rel_path: &str, extracted: &mut ExtractedImports) {
    let normalized = text.trim();
    let Some(rest) = normalized.strip_prefix("import ") else {
        extracted.imports.push(ImportRelation {
            file_path: rel_path.to_string(),
            module_name: normalized.to_string(),
        });
        return;
    };

    let mut tokens = rest.split_whitespace();
    let mut module_token = tokens.next().unwrap_or_default();
    if matches!(
        module_token,
        "class" | "struct" | "enum" | "protocol" | "func" | "typealias" | "var" | "let"
    ) {
        module_token = tokens.next().unwrap_or_default();
    }
    let module = module_token.split('.').next().unwrap_or_default();
    extracted.imports.push(ImportRelation {
        file_path: rel_path.to_string(),
        module_name: rest.to_string(),
    });
    if module.is_empty()
        || matches!(
            module,
            "class" | "struct" | "enum" | "protocol" | "func" | "typealias" | "var" | "let"
        )
    {
        return;
    }

    extracted.bindings.external_roots.insert(
        module.to_string(),
        ExternalRootBinding {
            module: module.to_string(),
            module_from_qualifier: false,
        },
    );
}

fn parse_ruby_import_statement(
    text: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    let normalized = text.trim();
    let Some(method) = normalized.split_whitespace().next() else {
        return;
    };

    let literal = extract_quoted_string(normalized);
    extracted.imports.push(ImportRelation {
        file_path: rel_path.to_string(),
        module_name: literal.clone().unwrap_or_else(|| normalized.to_string()),
    });

    if method != "require" {
        return;
    }
    let Some(required) = literal else {
        return;
    };
    let Some(root) = import_context.ruby_require_root(&required) else {
        return;
    };
    if import_context.ruby_local_constant_roots.contains(root) {
        return;
    }
    extracted.bindings.external_roots.insert(
        root.to_string(),
        ExternalRootBinding {
            module: required,
            module_from_qualifier: false,
        },
    );
}

fn parse_dart_import_statement(
    text: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    let normalized = collapse_whitespace(text);
    let Some(uri) = extract_quoted_string(&normalized) else {
        extracted.imports.push(ImportRelation {
            file_path: rel_path.to_string(),
            module_name: normalized,
        });
        return;
    };

    extracted.imports.push(ImportRelation {
        file_path: rel_path.to_string(),
        module_name: uri.clone(),
    });

    if !normalized.starts_with("import ") || !is_external_dart_uri(&uri, import_context) {
        return;
    }
    let Some(alias) = dart_import_alias(&normalized) else {
        return;
    };
    extracted.bindings.member.insert(alias, uri);
}

fn parse_elixir_import_statement(
    text: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    let normalized = collapse_whitespace(text);
    let Some((keyword, rest)) = normalized.split_once(' ') else {
        extracted.imports.push(ImportRelation {
            file_path: rel_path.to_string(),
            module_name: normalized,
        });
        return;
    };
    let target = rest.split([',', ' ']).next().unwrap_or_default().trim();
    extracted.imports.push(ImportRelation {
        file_path: rel_path.to_string(),
        module_name: if target.is_empty() {
            normalized.clone()
        } else {
            target.to_string()
        },
    });

    if !matches!(keyword, "alias" | "require") || !is_elixir_alias_path(target) {
        return;
    }
    let Some(root) = target.split('.').next() else {
        return;
    };
    if import_context.elixir_local_module_roots.contains(root) {
        return;
    }
    let Some(module) = import_context.elixir_external_root_module(root) else {
        return;
    };

    if keyword == "alias" {
        let alias = elixir_alias_as(&normalized)
            .unwrap_or_else(|| target.rsplit('.').next().unwrap_or(target).to_string());
        extracted.bindings.member.insert(alias, target.to_string());
    }
    extracted.bindings.external_roots.insert(
        root.to_string(),
        ExternalRootBinding {
            module: module.to_string(),
            module_from_qualifier: true,
        },
    );
}
