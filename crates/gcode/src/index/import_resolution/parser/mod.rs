use crate::models::{ImportRelation, Symbol};

use super::context::{
    ExternalCallTarget, ExternalRootBinding, ExtractedImports, ImportBindings,
    ImportResolutionContext,
};
use super::predicates::rust_external_roots;

mod go_rust;
mod java_csharp;
mod php_kotlin;
mod python_js;
mod rest;

use go_rust::{parse_go_import_statement, parse_rust_import_statement};
use java_csharp::{
    csharp_global_qualifier_parts, parse_csharp_import_statement, parse_java_import_statement,
};
use php_kotlin::{
    parse_kotlin_import_statement, parse_php_import_statement, php_local_symbol_exists,
};
use python_js::{parse_js_import_statement, parse_python_import_statement};
use rest::{
    parse_dart_import_statement, parse_elixir_import_statement, parse_ruby_import_statement,
    parse_swift_import_statement,
};

pub(crate) fn parse_import_statement(
    language: &str,
    text: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) -> anyhow::Result<()> {
    match language {
        "python" => parse_python_import_statement(text, rel_path, import_context, extracted),
        "javascript" | "typescript" => {
            parse_js_import_statement(text, rel_path, import_context, extracted)
        }
        "go" => parse_go_import_statement(text, rel_path, import_context, extracted)?,
        "rust" => parse_rust_import_statement(text, rel_path, import_context, extracted),
        "java" => parse_java_import_statement(text, rel_path, import_context, extracted),
        "csharp" => parse_csharp_import_statement(text, rel_path, import_context, extracted),
        "php" => parse_php_import_statement(text, rel_path, import_context, extracted),
        "kotlin" => parse_kotlin_import_statement(text, rel_path, import_context, extracted),
        "swift" => parse_swift_import_statement(text, rel_path, import_context, extracted),
        "ruby" => parse_ruby_import_statement(text, rel_path, import_context, extracted),
        "dart" => parse_dart_import_statement(text, rel_path, import_context, extracted),
        "elixir" => parse_elixir_import_statement(text, rel_path, import_context, extracted),
        _ => extracted.imports.push(ImportRelation {
            file_path: rel_path.to_string(),
            module_name: text.to_string(),
        }),
    }
    Ok(())
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
    if let Some(module) = qualifier_path.strip_prefix('\\') {
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
