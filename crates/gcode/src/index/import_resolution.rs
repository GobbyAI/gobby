use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

use crate::models::{ImportRelation, Symbol};

#[derive(Debug, Clone, Default)]
pub struct ImportResolutionContext {
    python_modules: HashSet<String>,
    js_external_packages: HashSet<String>,
    js_self_package_name: Option<String>,
    go_module_path: Option<String>,
    rust_external_crates: HashSet<String>,
    rust_self_crate_name: Option<String>,
    java_local_classes: HashSet<String>,
    csharp_local_roots: HashSet<String>,
}

#[derive(Debug, Clone)]
pub(crate) struct ExternalImportBinding {
    pub(crate) module: String,
    pub(crate) callee_name: String,
}

#[derive(Debug, Clone, Default)]
pub(crate) struct ImportBindings {
    pub(crate) bare: HashMap<String, ExternalImportBinding>,
    pub(crate) bare_wildcard_modules: Vec<String>,
    pub(crate) member: HashMap<String, String>,
    pub(crate) external_roots: HashMap<String, ExternalRootBinding>,
}

#[derive(Debug, Clone)]
pub(crate) struct ExternalRootBinding {
    pub(crate) module: String,
    pub(crate) module_from_qualifier: bool,
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
        go_module_path: load_go_module_path(root_path),
        rust_external_crates: load_rust_external_crates(root_path),
        rust_self_crate_name: load_rust_self_crate_name(root_path),
        java_local_classes: build_java_local_class_index(candidate_files),
        csharp_local_roots: build_csharp_local_roots(candidate_files),
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
        "go" => parse_go_import_statement(text, rel_path, import_context, extracted),
        "rust" => parse_rust_import_statement(text, rel_path, import_context, extracted),
        "java" => parse_java_import_statement(text, rel_path, import_context, extracted),
        "csharp" => parse_csharp_import_statement(text, rel_path, import_context, extracted),
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
    if language != "rust" {
        return;
    }

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

pub(crate) fn resolve_external_callee(
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

        let mut module = rel
            .with_extension("")
            .to_string_lossy()
            .replace(['/', '\\'], ".");
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

fn load_go_module_path(root_path: &Path) -> Option<String> {
    let contents = std::fs::read_to_string(root_path.join("go.mod")).ok()?;
    contents.lines().find_map(|line| {
        let line = line.trim();
        line.strip_prefix("module ")
            .map(str::trim)
            .filter(|module| !module.is_empty())
            .map(ToOwned::to_owned)
    })
}

fn load_rust_external_crates(root_path: &Path) -> HashSet<String> {
    let contents = std::fs::read_to_string(root_path.join("Cargo.toml")).unwrap_or_default();
    let mut crates = HashSet::new();
    let mut in_dependency_section = false;

    for line in contents.lines() {
        let line = line.split('#').next().unwrap_or(line).trim();
        if line.is_empty() {
            continue;
        }
        if line.starts_with('[') && line.ends_with(']') {
            let section = line.trim_matches(['[', ']']);
            in_dependency_section = matches!(
                section,
                "dependencies" | "dev-dependencies" | "build-dependencies"
            ) || section.ends_with(".dependencies")
                || section.ends_with(".dev-dependencies")
                || section.ends_with(".build-dependencies");
            continue;
        }
        if !in_dependency_section {
            continue;
        }
        let Some((name, _)) = line.split_once('=') else {
            continue;
        };
        let name = name.trim().trim_matches('"').replace('-', "_");
        if !name.is_empty() {
            crates.insert(name);
        }
    }

    crates
}

fn load_rust_self_crate_name(root_path: &Path) -> Option<String> {
    let contents = std::fs::read_to_string(root_path.join("Cargo.toml")).ok()?;
    let mut in_package_section = false;

    for line in contents.lines() {
        let line = line.split('#').next().unwrap_or(line).trim();
        if line.is_empty() {
            continue;
        }
        if line.starts_with('[') && line.ends_with(']') {
            in_package_section = line == "[package]";
            continue;
        }
        if !in_package_section {
            continue;
        }
        let Some((name, value)) = line.split_once('=') else {
            continue;
        };
        if name.trim() == "name" {
            return Some(value.trim().trim_matches('"').replace('-', "_"));
        }
    }

    None
}

fn build_java_local_class_index(candidate_files: &[PathBuf]) -> HashSet<String> {
    let mut classes = HashSet::new();
    for path in candidate_files {
        let ext = path
            .extension()
            .and_then(|ext| ext.to_str())
            .unwrap_or_default();
        if ext != "java" {
            continue;
        }
        let Ok(contents) = std::fs::read_to_string(path) else {
            continue;
        };
        let package = contents.lines().find_map(|line| {
            let line = line.trim();
            line.strip_prefix("package ")
                .map(|rest| rest.trim().trim_end_matches(';').trim().to_string())
        });
        for class_name in java_declared_types(&contents) {
            classes.insert(class_name.clone());
            if let Some(package) = package.as_deref()
                && !package.is_empty()
            {
                classes.insert(format!("{package}.{class_name}"));
            }
        }
    }
    classes
}

fn build_csharp_local_roots(candidate_files: &[PathBuf]) -> HashSet<String> {
    let mut roots = HashSet::new();
    for path in candidate_files {
        let ext = path
            .extension()
            .and_then(|ext| ext.to_str())
            .unwrap_or_default();
        if ext != "cs" {
            continue;
        }
        let Ok(contents) = std::fs::read_to_string(path) else {
            continue;
        };
        for line in contents.lines() {
            let line = line.trim();
            if let Some(rest) = line.strip_prefix("namespace ") {
                let namespace = rest
                    .trim()
                    .trim_end_matches([';', '{'])
                    .split_whitespace()
                    .next()
                    .unwrap_or_default();
                if let Some(root) = namespace.split('.').next()
                    && !root.is_empty()
                {
                    roots.insert(root.to_string());
                }
            }
        }
        for type_name in java_declared_types(&contents) {
            roots.insert(type_name);
        }
    }
    roots
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

    if rest.contains('*') || rest.contains('{') || rest.contains('}') {
        return;
    }

    let (path, alias) = split_alias(rest);
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
        let target = target.trim();
        extracted.imports.push(ImportRelation {
            file_path: rel_path.to_string(),
            module_name: target.to_string(),
        });
        if is_external_csharp_path(target, import_context) {
            extracted
                .bindings
                .bare_wildcard_modules
                .push(target.to_string());
        }
        return;
    }

    if let Some((alias, target)) = rest.split_once('=') {
        let alias = alias.trim();
        let target = target.trim();
        extracted.imports.push(ImportRelation {
            file_path: rel_path.to_string(),
            module_name: target.to_string(),
        });
        if !alias.is_empty() && is_external_csharp_path(target, import_context) {
            extracted
                .bindings
                .member
                .insert(alias.to_string(), target.to_string());
        }
        return;
    }

    let namespace = rest.trim();
    extracted.imports.push(ImportRelation {
        file_path: rel_path.to_string(),
        module_name: namespace.to_string(),
    });
    if !is_external_csharp_path(namespace, import_context) {
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

fn go_default_package_alias(module: &str) -> String {
    module
        .trim_end_matches('/')
        .rsplit('/')
        .next()
        .unwrap_or(module)
        .split_once(".v")
        .map(|(name, _)| name)
        .unwrap_or_else(|| module.rsplit('/').next().unwrap_or(module))
        .replace('-', "_")
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

fn is_external_go_module(module: &str, import_context: &ImportResolutionContext) -> bool {
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

fn rust_external_roots(import_context: &ImportResolutionContext) -> HashSet<String> {
    let mut roots = import_context.rust_external_crates.clone();
    roots.extend(
        ["std", "core", "alloc", "proc_macro", "test"]
            .into_iter()
            .map(ToOwned::to_owned),
    );
    if let Some(self_crate) = import_context.rust_self_crate_name.as_deref() {
        roots.remove(self_crate);
    }
    roots
}

fn java_declared_types(contents: &str) -> Vec<String> {
    let mut names = Vec::new();
    let tokens: Vec<&str> = contents
        .split(|ch: char| !(ch.is_ascii_alphanumeric() || ch == '_'))
        .filter(|token| !token.is_empty())
        .collect();
    for window in tokens.windows(2) {
        if matches!(window[0], "class" | "interface" | "enum" | "record") {
            names.push(window[1].to_string());
        }
    }
    names
}

fn is_external_java_class(class_path: &str, import_context: &ImportResolutionContext) -> bool {
    !import_context.java_local_classes.contains(class_path)
        && class_path
            .rsplit('.')
            .next()
            .is_none_or(|class_name| !import_context.java_local_classes.contains(class_name))
}

fn is_external_csharp_path(path: &str, import_context: &ImportResolutionContext) -> bool {
    path.split('.')
        .next()
        .is_some_and(|root| !import_context.csharp_local_roots.contains(root))
}

fn is_external_rust_root(root: &str, import_context: &ImportResolutionContext) -> bool {
    if matches!(root, "crate" | "self" | "super") {
        return false;
    }
    if import_context.rust_self_crate_name.as_deref() == Some(root) {
        return false;
    }
    import_context.rust_external_crates.contains(root)
        || matches!(root, "std" | "core" | "alloc" | "proc_macro" | "test")
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
