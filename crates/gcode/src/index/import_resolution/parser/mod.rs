use crate::models::{ImportRelation, Symbol};

use super::UNPARSED_IMPORT_PREFIX;
use super::context::{
    ExternalCallTarget, ExternalRootBinding, ExtractedImports, ImportBindings,
    ImportResolutionContext, LocalCallBinding,
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
        "python" => parse_python_import_statement(text, rel_path, import_context, extracted)?,
        "javascript" | "typescript" => {
            parse_js_import_statement(text, rel_path, import_context, extracted)?
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
        _ => push_unparsed_import(rel_path, text, extracted)?,
    }
    Ok(())
}

pub(super) fn push_unparsed_import(
    rel_path: &str,
    text: &str,
    extracted: &mut ExtractedImports,
) -> anyhow::Result<()> {
    let text = text.trim();
    if text.is_empty() {
        anyhow::bail!("unparsed import fallback for `{rel_path}` was empty");
    }
    if text.lines().count() != 1 {
        anyhow::bail!("unparsed import fallback for `{rel_path}` must be a single line");
    }
    log::debug!("recording unparsed import fallback in {rel_path}: {text}");
    extracted.imports.push(ImportRelation {
        file_path: rel_path.to_string(),
        module_name: format!("{UNPARSED_IMPORT_PREFIX}{text}"),
    });
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
        // Multiple wildcard imports make the source module ambiguous. Picking
        // any one would create a deterministic-looking but false graph edge.
        if import_bindings.bare_wildcard_modules.len() > 1 {
            log::debug!(
                "skipping ambiguous bare call `{callee_name}` with {} wildcard imports",
                import_bindings.bare_wildcard_modules.len()
            );
        }
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

pub(crate) fn resolve_local_callee(
    import_bindings: &ImportBindings,
    symbols: &[Symbol],
    callee_name: &str,
    is_bare_call: bool,
) -> Option<LocalCallBinding> {
    if !is_bare_call {
        return None;
    }
    if symbols.iter().any(|symbol| symbol.name == callee_name) {
        return None;
    }
    import_bindings.local_bare.get(callee_name).cloned()
}

pub(crate) fn resolve_local_member_callee(
    import_bindings: &ImportBindings,
    symbols: &[Symbol],
    callee_name: &str,
    root_alias: Option<&str>,
    is_member_call: bool,
) -> Option<LocalCallBinding> {
    if !is_member_call {
        return None;
    }
    let root_alias = root_alias?;
    if symbols.iter().any(|symbol| symbol.name == root_alias) {
        return None;
    }
    let candidate_files = import_bindings.local_member.get(root_alias)?.clone();
    Some(LocalCallBinding {
        candidate_files,
        callee_name: callee_name.to_string(),
    })
}

/// Resolves a Ruby member call (`Widget.build`, `Widget.new`) against the files
/// that locally declare the receiver constant. Ruby `require`/`require_relative`
/// loads files without importing names, so the receiver constant — not an import
/// binding — drives resolution: `ruby_constant_files` maps a constant root to its
/// declaring files. `Widget.new` constructs the class, so it resolves to the
/// class symbol (named for the constant) rather than a nonexistent `new` method;
/// every other member resolves to the named method. The post-write DB pass
/// narrows the candidate files to the real `code_symbols` id (or degrades to
/// unresolved), so a wrong-constant collision can never produce a false edge.
pub(crate) fn resolve_ruby_local_member_callee(
    import_context: &ImportResolutionContext,
    symbols: &[Symbol],
    callee_name: &str,
    root_alias: Option<&str>,
    is_member_call: bool,
) -> Option<LocalCallBinding> {
    if !is_member_call {
        return None;
    }
    let root_alias = root_alias?;
    // A same-file constant shadows the cross-file one; let same-file resolution
    // (or unresolved) own it, mirroring the other member resolvers.
    if symbols.iter().any(|symbol| symbol.name == root_alias) {
        return None;
    }
    let candidate_files = import_context.ruby_constant_files(root_alias);
    if candidate_files.is_empty() {
        return None;
    }
    let target_name = if callee_name == "new" {
        root_alias.to_string()
    } else {
        callee_name.to_string()
    };
    Some(LocalCallBinding {
        candidate_files,
        callee_name: target_name,
    })
}

/// Resolve a fully-qualified PHP static call (`\Ns\Class::method()`) against the
/// file(s) that locally declare the qualifier's class. A PHP `\Ns\Class`
/// qualifier *is* the class, so it is looked up directly in `php_symbol_files`
/// (case-insensitively, since PHP class names are case-insensitive). A bare
/// `Class::m()` is left to the `use`-provenance `local_member` channel, so this
/// resolver only fires when the qualifier is namespace-qualified. Returns `None`
/// (leaving the call unresolved) when the class is not locally declared. The
/// post-write DB pass narrows the candidate file(s) to the real method symbol,
/// so a non-local qualifier can never produce a false edge.
pub(crate) fn resolve_php_local_member_callee(
    import_context: &ImportResolutionContext,
    callee_name: &str,
    qualifier_path: Option<&str>,
    is_member_call: bool,
) -> Option<LocalCallBinding> {
    if !is_member_call {
        return None;
    }
    let class_path = qualifier_path?.trim_start_matches('\\');
    if !class_path.contains('\\') {
        return None;
    }
    let candidate_files = import_context.php_candidate_files(class_path);
    if candidate_files.is_empty() {
        return None;
    }
    Some(LocalCallBinding {
        candidate_files,
        callee_name: callee_name.to_string(),
    })
}

/// Resolves a *bare* Swift call against the caller's own module. Swift has
/// whole-module scope: files in a module call each other with no `import`
/// statement, so there is nothing import-bound to seed candidates from. Instead
/// the candidate files are every `.swift` file sharing a module with the caller
/// (`rel_path`), and the post-write DB pass narrows the bare `callee_name` to a
/// single function/type/method id (or degrades to unresolved on ambiguity).
/// Only fires for bare calls — member calls (`obj.method()`, `Type.member()`)
/// stay unresolved, so a dynamic or qualified receiver can never produce a false
/// edge.
pub(crate) fn resolve_swift_local_callee(
    import_context: &ImportResolutionContext,
    rel_path: &str,
    callee_name: &str,
    is_bare_call: bool,
) -> Option<LocalCallBinding> {
    if !is_bare_call {
        return None;
    }
    let candidate_files = import_context.swift_module_candidate_files(rel_path);
    if candidate_files.is_empty() {
        return None;
    }
    Some(LocalCallBinding {
        candidate_files,
        callee_name: callee_name.to_string(),
    })
}

/// Resolves a bare Dart call against the files brought into scope by this file's
/// unaliased local imports. The candidate set comes from `dart_local_import_files`
/// (populated at import-parse time); the bare call name is the target symbol
/// name. The post-write DB pass narrows the candidates to a real id, so a name
/// that no imported file declares simply degrades to unresolved — never a
/// phantom edge. Aliased prefixed calls (`p.name()`) and member calls resolve
/// elsewhere (`resolve_local_member_callee`).
pub(crate) fn resolve_dart_local_callee(
    import_bindings: &ImportBindings,
    symbols: &[Symbol],
    callee_name: &str,
    is_bare_call: bool,
) -> Option<LocalCallBinding> {
    if !is_bare_call {
        return None;
    }
    if import_bindings.dart_local_import_files.is_empty() {
        return None;
    }
    // A same-file top-level declaration is resolved by the same-file pass; never
    // shadow it with a cross-file candidate set.
    if symbols.iter().any(|symbol| symbol.name == callee_name) {
        return None;
    }
    let mut candidate_files = import_bindings.dart_local_import_files.clone();
    candidate_files.sort();
    candidate_files.dedup();
    Some(LocalCallBinding {
        candidate_files,
        callee_name: callee_name.to_string(),
    })
}

/// Resolve an Elixir call against a locally-declared module's file(s).
///
/// Handles two shapes the shared channels do not:
///
/// * **Fully-qualified** `App.Foo.func()` — the qualifier is the module's
///   fully-qualified name, looked up directly in `elixir_module_files`. (Aliased
///   `Foo.func()` after `alias App.Foo` resolves through the shared
///   `local_member` channel instead.)
/// * **Bare** `func()` after a local `import App.Foo` — the candidate set comes
///   from `elixir_local_import_files`; the bare call name is the target symbol
///   name.
///
/// Returns `None` (leaving the call unresolved) when the module is not locally
/// declared, no local import is in scope, or a same-file symbol already defines
/// the bare name. The post-write DB pass narrows the candidate files to the real
/// symbol, so an unmatched name degrades to unresolved rather than a phantom edge.
pub(crate) fn resolve_elixir_local_callee(
    import_context: &ImportResolutionContext,
    import_bindings: &ImportBindings,
    symbols: &[Symbol],
    callee_name: &str,
    qualifier_path: Option<&str>,
    is_bare_call: bool,
    is_member_call: bool,
) -> Option<LocalCallBinding> {
    if is_member_call {
        let candidate_files = import_context.elixir_module_files(qualifier_path?);
        if candidate_files.is_empty() {
            return None;
        }
        return Some(LocalCallBinding {
            candidate_files,
            callee_name: callee_name.to_string(),
        });
    }
    if !is_bare_call {
        return None;
    }
    if import_bindings.elixir_local_import_files.is_empty() {
        return None;
    }
    // A same-file top-level declaration is resolved by the same-file pass; never
    // shadow it with a cross-file candidate set.
    if symbols.iter().any(|symbol| symbol.name == callee_name) {
        return None;
    }
    let mut candidate_files = import_bindings.elixir_local_import_files.clone();
    candidate_files.sort();
    candidate_files.dedup();
    Some(LocalCallBinding {
        candidate_files,
        callee_name: callee_name.to_string(),
    })
}

pub(crate) fn resolve_rust_local_qualified_callee(
    import_context: &ImportResolutionContext,
    rel_path: &str,
    callee_name: &str,
    qualifier_path: Option<&str>,
    is_member_call: bool,
) -> Option<LocalCallBinding> {
    if !is_member_call {
        return None;
    }
    let qualifier_path = qualifier_path?;
    import_context.rust_qualified_candidate(rel_path, qualifier_path, callee_name)
}

/// Resolve a C# member call against a locally-declared type's file(s).
///
/// Handles two qualifier shapes that the alias channel (`local_member`) does not
/// cover:
///
/// * **Fully-qualified** `Ns.Type.M()` — the qualifier is the type's
///   fully-qualified name, looked up directly in `csharp_type_files`.
/// * **Namespace-imported simple type** `Type.M()` after `using Ns;` — the
///   single-segment qualifier is resolved against each locally imported
///   namespace (`Ns.Type`), merging the candidate files across namespaces.
///
/// Returns `None` (leaving the call unresolved) when the type is not locally
/// declared or a same-file symbol shadows the qualifier root. The post-write DB
/// pass narrows the candidate files to the real method symbol.
pub(crate) fn resolve_csharp_local_member_callee(
    import_context: &ImportResolutionContext,
    import_bindings: &ImportBindings,
    symbols: &[Symbol],
    callee_name: &str,
    root_alias: Option<&str>,
    qualifier_path: Option<&str>,
    is_member_call: bool,
) -> Option<LocalCallBinding> {
    if !is_member_call {
        return None;
    }
    let qualifier_path = qualifier_path?;
    let root_alias = root_alias?;
    if symbols.iter().any(|symbol| symbol.name == root_alias) {
        return None;
    }
    let candidate_files = if qualifier_path.contains('.') {
        import_context.csharp_type_files(qualifier_path)
    } else {
        let mut files: Vec<String> = import_bindings
            .csharp_local_namespaces
            .iter()
            .flat_map(|namespace| {
                import_context.csharp_type_files(&format!("{namespace}.{qualifier_path}"))
            })
            .collect();
        files.sort();
        files.dedup();
        files
    };
    if candidate_files.is_empty() {
        return None;
    }
    Some(LocalCallBinding {
        candidate_files,
        callee_name: callee_name.to_string(),
    })
}
