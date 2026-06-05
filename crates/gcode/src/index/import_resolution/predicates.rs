use std::collections::{HashMap, HashSet};
use std::sync::OnceLock;

use super::context::{ImportResolutionContext, JS_BUILTIN_MODULES};

const STANDARD_RUST_CRATES: &[&str] = &["std", "core", "alloc", "proc_macro", "test"];

pub(super) fn is_external_python_module(
    module: &str,
    import_context: &ImportResolutionContext,
) -> bool {
    if module.starts_with('.') {
        return false;
    }

    !import_context.python_modules.iter().any(|local_module| {
        local_module == module
            || local_module.starts_with(&format!("{module}."))
            || module.starts_with(&format!("{local_module}."))
    })
}

pub(super) fn is_external_js_module(
    module: &str,
    import_context: &ImportResolutionContext,
) -> bool {
    if module.starts_with("node:") {
        return true;
    }
    if module.starts_with("./")
        || module.starts_with("../")
        || module.starts_with('/')
        || module.starts_with('#')
        || module.starts_with("~/")
        || module.starts_with("@/")
    {
        return false;
    }

    if JS_BUILTIN_MODULES.contains(&module) {
        return true;
    }

    let Some(package_name) = js_package_name(module) else {
        return false;
    };
    if import_context.js_self_package_name.as_deref() == Some(package_name) {
        return false;
    }

    import_context.js_external_packages.contains(package_name)
        || JS_BUILTIN_MODULES.contains(&package_name)
}

pub(super) fn is_external_go_module(
    module: &str,
    import_context: &ImportResolutionContext,
) -> bool {
    if module.starts_with('.') {
        return false;
    }
    if let Some(self_module) = import_context.go_module_path.as_deref()
        && (module == self_module || module.starts_with(&format!("{self_module}/")))
    {
        return false;
    }
    true
}

pub(super) fn rust_external_roots(import_context: &ImportResolutionContext) -> HashSet<String> {
    let mut roots = import_context.rust_external_crates.clone();
    roots.extend(STANDARD_RUST_CRATES.iter().copied().map(ToOwned::to_owned));
    if let Some(self_crate) = import_context.rust_self_crate_name.as_deref() {
        roots.remove(self_crate);
    }
    roots
}

pub(super) fn java_declared_types(contents: &str) -> Vec<String> {
    declared_types(contents, &["class", "interface", "enum", "record"])
}

pub(super) fn csharp_declared_types(contents: &str) -> Vec<String> {
    declared_types(
        contents,
        &["class", "interface", "enum", "record", "struct"],
    )
}

/// Heuristic fallback scanner for import-resolution local symbol indexes.
///
/// The indexer records authoritative symbols from tree-sitter; this token scan
/// only helps decide whether an otherwise unresolved import target is local.
pub(super) fn declared_types(contents: &str, keywords: &[&str]) -> Vec<String> {
    let contents = strip_comments_and_string_literals(contents);
    let mut names = Vec::new();
    let tokens: Vec<&str> = contents
        .split(|ch: char| !(ch.is_ascii_alphanumeric() || ch == '_'))
        .filter(|token| !token.is_empty())
        .collect();
    for window in tokens.windows(2) {
        if keywords.contains(&window[0]) {
            names.push(window[1].to_string());
        }
    }
    names
}

fn strip_comments_and_string_literals(contents: &str) -> String {
    let mut out = String::with_capacity(contents.len());
    let mut chars = contents.chars().peekable();
    while let Some(ch) = chars.next() {
        match ch {
            '/' if chars.peek() == Some(&'/') => {
                out.push(' ');
                out.push(' ');
                chars.next();
                for ch in chars.by_ref() {
                    if ch == '\n' {
                        out.push('\n');
                        break;
                    }
                    out.push(' ');
                }
            }
            '/' if chars.peek() == Some(&'*') => {
                out.push(' ');
                out.push(' ');
                chars.next();
                let mut previous = '\0';
                for ch in chars.by_ref() {
                    if ch == '\n' {
                        out.push('\n');
                    } else {
                        out.push(' ');
                    }
                    if previous == '*' && ch == '/' {
                        break;
                    }
                    previous = ch;
                }
            }
            '"' | '\'' | '`' => {
                let quote = ch;
                let out_len_before_literal = out.len();
                out.push(' ');
                let mut terminated = false;
                while let Some(ch) = chars.next() {
                    if ch == '\\' {
                        out.push(' ');
                        if let Some(escaped) = chars.next() {
                            out.push(if escaped == '\n' { '\n' } else { ' ' });
                        }
                        continue;
                    }
                    if ch == quote {
                        out.push(' ');
                        terminated = true;
                        break;
                    }
                    if ch == '\n' {
                        out.push('\n');
                        if quote != '`' {
                            break;
                        }
                    } else {
                        out.push(' ');
                    }
                }
                if !terminated {
                    out.truncate(out_len_before_literal);
                    break;
                }
            }
            _ => out.push(ch),
        }
    }
    out
}

/// Heuristic fallback scanner for PHP import-resolution local symbol indexes.
///
/// The indexer records authoritative symbols from tree-sitter; this token scan
/// only helps decide whether an otherwise unresolved import target is local.
pub(super) fn php_declared_symbols(contents: &str) -> Vec<String> {
    let cleaned = strip_comments_and_string_literals(contents);
    let mut names = Vec::new();
    let tokens: Vec<&str> = cleaned
        .split(|ch: char| !(ch.is_ascii_alphanumeric() || ch == '_'))
        .filter(|token| !token.is_empty())
        .collect();
    for window in tokens.windows(2) {
        if matches!(
            window[0],
            "class" | "interface" | "trait" | "enum" | "function"
        ) {
            names.push(window[1].to_string());
        }
    }
    names
}

pub(super) fn is_external_java_class(
    class_path: &str,
    import_context: &ImportResolutionContext,
) -> bool {
    java_class_name_candidates(class_path)
        .filter(|candidate| !candidate.is_empty())
        .all(|candidate| !import_context.java_local_classes.contains(candidate))
}

fn java_class_name_candidates(class_path: &str) -> impl Iterator<Item = &str> {
    let dotted = class_path.rsplit('.').next().unwrap_or(class_path);
    [
        class_path,
        dotted,
        dotted.rsplit('$').next().unwrap_or(dotted),
    ]
    .into_iter()
}

pub(super) fn is_external_csharp_path(
    path: &str,
    import_context: &ImportResolutionContext,
) -> bool {
    path.strip_prefix("global::")
        .unwrap_or(path)
        .split('.')
        .next()
        .is_some_and(|root| !import_context.csharp_local_roots.contains(root))
}

pub(super) fn is_external_php_symbol(path: &str, import_context: &ImportResolutionContext) -> bool {
    let path_lower = path.to_ascii_lowercase();
    !import_context.php_local_symbols.contains(&path_lower)
        && path.rsplit('\\').next().is_none_or(|name| {
            !import_context
                .php_local_symbols
                .contains(&name.to_ascii_lowercase())
        })
}

pub(super) fn is_external_rust_root(root: &str, import_context: &ImportResolutionContext) -> bool {
    if matches!(root, "crate" | "self" | "super") {
        return false;
    }
    if import_context.rust_self_crate_name.as_deref() == Some(root) {
        return false;
    }
    import_context.rust_external_crates.contains(root) || STANDARD_RUST_CRATES.contains(&root)
}

/// Returns a curated Ruby `require` to constant-root mapping.
///
/// Keep the bundled map small and documented in
/// `assets/import_roots/ruby_require_roots.json`; use runtime overrides when
/// constructing `ImportResolutionContext` for project-specific roots.
pub(super) fn ruby_require_root(required: &str) -> Option<&'static str> {
    bundled_ruby_require_roots()
        .get(required)
        .map(String::as_str)
}

pub(super) fn is_external_dart_uri(uri: &str, import_context: &ImportResolutionContext) -> bool {
    if uri.starts_with("dart:") {
        return true;
    }
    let Some(package) = uri
        .strip_prefix("package:")
        .and_then(|rest| rest.split('/').next())
    else {
        return false;
    };
    import_context.dart_self_package_name.as_deref() != Some(package)
        && import_context.dart_external_packages.contains(package)
}

/// Returns curated Elixir dependency module roots.
///
/// Hex package names do not mechanically map to Elixir module roots. Maintain
/// the bundled map in `assets/import_roots/elixir_dependency_roots.json`; use
/// runtime overrides when constructing `ImportResolutionContext` if discovery
/// can provide more precise roots.
pub(super) fn elixir_dependency_roots(dep: &str) -> Option<&'static [String]> {
    bundled_elixir_dependency_roots()
        .get(dep)
        .map(Vec::as_slice)
}

pub(super) fn bundled_ruby_require_roots() -> &'static HashMap<String, String> {
    static ROOTS: OnceLock<HashMap<String, String>> = OnceLock::new();
    ROOTS.get_or_init(|| {
        serde_json::from_str(include_str!(
            "../../../assets/import_roots/ruby_require_roots.json"
        ))
        .expect(
            "failed to parse bundled import roots asset \
             ../../../assets/import_roots/ruby_require_roots.json; \
             please report this gobby-code packaging bug",
        )
    })
}

pub(super) fn bundled_elixir_dependency_roots() -> &'static HashMap<String, Vec<String>> {
    static ROOTS: OnceLock<HashMap<String, Vec<String>>> = OnceLock::new();
    ROOTS.get_or_init(|| {
        serde_json::from_str(include_str!(
            "../../../assets/import_roots/elixir_dependency_roots.json"
        ))
        .expect(
            "failed to parse bundled import roots asset \
             ../../../assets/import_roots/elixir_dependency_roots.json; \
             please report this gobby-code packaging bug",
        )
    })
}

pub(super) fn js_package_name(module: &str) -> Option<&str> {
    if let Some(stripped) = module.strip_prefix('@') {
        let mut segments = stripped.split('/');
        let scope = segments.next()?;
        let package = segments.next()?;
        let consumed = scope.len() + package.len() + 2;
        module.get(..consumed)
    } else {
        module.split('/').next()
    }
}
