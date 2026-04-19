use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

use crate::models::{ImportRelation, Symbol};

#[derive(Debug, Clone, Default)]
pub struct ImportResolutionContext {
    python_modules: HashSet<String>,
    js_external_packages: HashSet<String>,
    js_self_package_name: Option<String>,
}

#[derive(Debug, Clone)]
pub(crate) struct ExternalImportBinding {
    pub(crate) module: String,
    pub(crate) callee_name: String,
}

#[derive(Debug, Clone, Default)]
pub(crate) struct ImportBindings {
    pub(crate) bare: HashMap<String, ExternalImportBinding>,
    pub(crate) member: HashMap<String, String>,
}

#[derive(Debug, Clone, Default)]
pub(crate) struct ExtractedImports {
    pub(crate) imports: Vec<ImportRelation>,
    pub(crate) bindings: ImportBindings,
}

#[derive(Debug, Clone)]
pub(crate) struct ExternalCallTarget {
    pub(crate) module: String,
    pub(crate) callee_name: String,
}

const JS_BUILTIN_MODULES: &[&str] = &[
    "assert",
    "buffer",
    "child_process",
    "cluster",
    "crypto",
    "dgram",
    "dns",
    "domain",
    "events",
    "fs",
    "http",
    "https",
    "net",
    "os",
    "path",
    "perf_hooks",
    "process",
    "punycode",
    "querystring",
    "readline",
    "repl",
    "stream",
    "string_decoder",
    "timers",
    "tls",
    "trace_events",
    "tty",
    "url",
    "util",
    "v8",
    "vm",
    "worker_threads",
    "zlib",
];

pub fn build_import_resolution_context(
    root_path: &Path,
    candidate_files: &[PathBuf],
) -> ImportResolutionContext {
    ImportResolutionContext {
        python_modules: build_python_module_index(root_path, candidate_files),
        js_external_packages: load_js_external_packages(root_path),
        js_self_package_name: load_js_self_package_name(root_path),
    }
}

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
        _ => extracted.imports.push(ImportRelation {
            file_path: rel_path.to_string(),
            module_name: text.to_string(),
        }),
    }
}

pub(crate) fn resolve_external_callee(
    import_bindings: &ImportBindings,
    symbols: &[Symbol],
    callee_name: &str,
    root_alias: Option<&str>,
    is_bare_call: bool,
) -> Option<ExternalCallTarget> {
    if is_bare_call {
        let binding = import_bindings.bare.get(callee_name)?;
        if symbols.iter().any(|symbol| symbol.name == callee_name) {
            return None;
        }
        return Some(ExternalCallTarget {
            module: binding.module.clone(),
            callee_name: binding.callee_name.clone(),
        });
    }

    let root_alias = root_alias?;
    if symbols.iter().any(|symbol| symbol.name == root_alias) {
        return None;
    }
    let module = import_bindings.member.get(root_alias)?;
    Some(ExternalCallTarget {
        module: module.clone(),
        callee_name: callee_name.to_string(),
    })
}

fn build_python_module_index(root_path: &Path, candidate_files: &[PathBuf]) -> HashSet<String> {
    let mut modules = HashSet::new();

    for path in candidate_files {
        let Ok(rel) = path.strip_prefix(root_path) else {
            continue;
        };
        let ext = rel
            .extension()
            .and_then(|ext| ext.to_str())
            .unwrap_or_default()
            .to_ascii_lowercase();
        if !matches!(ext.as_str(), "py" | "pyi") {
            continue;
        }

        let mut module = rel.with_extension("").to_string_lossy().replace('/', ".");
        if module.ends_with(".__init__") {
            module.truncate(module.len() - ".__init__".len());
        }
        if module.is_empty() {
            continue;
        }
        modules.insert(module.clone());

        if let Some(stripped) = module.strip_prefix("src.") {
            modules.insert(stripped.to_string());
        }
    }

    modules
}

fn load_js_external_packages(root_path: &Path) -> HashSet<String> {
    let package_json = root_path.join("package.json");
    let Ok(contents) = std::fs::read_to_string(package_json) else {
        return HashSet::new();
    };
    let Ok(json) = serde_json::from_str::<serde_json::Value>(&contents) else {
        return HashSet::new();
    };

    let mut packages = HashSet::new();
    for field in [
        "dependencies",
        "devDependencies",
        "peerDependencies",
        "optionalDependencies",
        "bundledDependencies",
    ] {
        if let Some(map) = json.get(field).and_then(|value| value.as_object()) {
            packages.extend(map.keys().cloned());
        }
    }
    packages
}

fn load_js_self_package_name(root_path: &Path) -> Option<String> {
    let package_json = root_path.join("package.json");
    let contents = std::fs::read_to_string(package_json).ok()?;
    let json = serde_json::from_str::<serde_json::Value>(&contents).ok()?;
    json.get("name")
        .and_then(|value| value.as_str())
        .map(ToOwned::to_owned)
}

fn parse_python_import_statement(
    text: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    if let Some(rest) = text.strip_prefix("import ") {
        for entry in split_top_level(rest, ',') {
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
    for entry in split_top_level(imported, ',') {
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

    for part in split_top_level(clause, ',') {
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
            for item in split_top_level(inner, ',') {
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

fn collapse_whitespace(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn extract_js_module_specifier(text: &str) -> Option<String> {
    if let Some((_, after_from)) = text.rsplit_once(" from ") {
        return extract_quoted_string(after_from);
    }
    let rest = text.strip_prefix("import ")?;
    extract_quoted_string(rest)
}

fn extract_js_import_clause(text: &str) -> Option<&str> {
    let rest = text.strip_prefix("import ")?;
    let (clause, _) = rest.rsplit_once(" from ")?;
    Some(clause)
}

fn extract_quoted_string(text: &str) -> Option<String> {
    let quote = text.find(['"', '\''])?;
    let quote_char = text[quote..].chars().next()?;
    let after_quote = &text[quote + quote_char.len_utf8()..];
    let end = after_quote.find(quote_char)?;
    Some(after_quote[..end].to_string())
}

fn split_alias(text: &str) -> (&str, Option<&str>) {
    if let Some((name, alias)) = text.split_once(" as ") {
        (name.trim(), Some(alias.trim()))
    } else {
        (text.trim(), None)
    }
}

fn split_top_level(text: &str, delimiter: char) -> Vec<&str> {
    let mut parts = Vec::new();
    let mut start = 0;
    let mut paren_depth = 0usize;
    let mut brace_depth = 0usize;
    let mut bracket_depth = 0usize;
    let mut in_single = false;
    let mut in_double = false;

    for (idx, ch) in text.char_indices() {
        match ch {
            '\'' if !in_double => in_single = !in_single,
            '"' if !in_single => in_double = !in_double,
            '(' if !in_single && !in_double => paren_depth += 1,
            ')' if !in_single && !in_double && paren_depth > 0 => paren_depth -= 1,
            '{' if !in_single && !in_double => brace_depth += 1,
            '}' if !in_single && !in_double && brace_depth > 0 => brace_depth -= 1,
            '[' if !in_single && !in_double => bracket_depth += 1,
            ']' if !in_single && !in_double && bracket_depth > 0 => bracket_depth -= 1,
            ch if ch == delimiter
                && !in_single
                && !in_double
                && paren_depth == 0
                && brace_depth == 0
                && bracket_depth == 0 =>
            {
                parts.push(text[start..idx].trim());
                start = idx + ch.len_utf8();
            }
            _ => {}
        }
    }

    if start <= text.len() {
        parts.push(text[start..].trim());
    }

    parts
}

fn is_external_python_module(module: &str, import_context: &ImportResolutionContext) -> bool {
    if module.starts_with('.') {
        return false;
    }

    !import_context.python_modules.iter().any(|local_module| {
        local_module == module
            || local_module.starts_with(&format!("{module}."))
            || module.starts_with(&format!("{local_module}."))
    })
}

fn is_external_js_module(module: &str, import_context: &ImportResolutionContext) -> bool {
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

    let Some(package_name) = js_package_name(module) else {
        return false;
    };
    if import_context.js_self_package_name.as_deref() == Some(package_name) {
        return false;
    }

    import_context.js_external_packages.contains(package_name)
        || JS_BUILTIN_MODULES.contains(&package_name)
}

fn js_package_name(module: &str) -> Option<&str> {
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
