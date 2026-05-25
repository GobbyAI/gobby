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
    php_local_symbols: HashSet<String>,
    ruby_local_constant_roots: HashSet<String>,
    dart_external_packages: HashSet<String>,
    dart_self_package_name: Option<String>,
    elixir_external_roots: HashMap<String, String>,
    elixir_local_module_roots: HashSet<String>,
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
        php_local_symbols: build_php_local_symbol_index(candidate_files),
        ruby_local_constant_roots: build_ruby_local_constant_roots(candidate_files),
        dart_external_packages: load_dart_external_packages(root_path),
        dart_self_package_name: load_dart_self_package_name(root_path),
        elixir_external_roots: load_elixir_external_roots(root_path),
        elixir_local_module_roots: build_elixir_local_module_roots(candidate_files),
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
        "php" => parse_php_import_statement(text, rel_path, import_context, extracted),
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
        return Some(ExternalCallTarget {
            module: module.to_string(),
            callee_name: callee_name.to_string(),
        });
    }
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
    let Ok(contents) = std::fs::read_to_string(root_path.join("Cargo.toml")) else {
        return HashSet::new();
    };
    let Ok(cargo_toml) = toml::from_str::<toml::Table>(&contents) else {
        return HashSet::new();
    };
    let mut crates = HashSet::new();

    for section in ["dependencies", "dev-dependencies", "build-dependencies"] {
        collect_rust_dependency_keys(cargo_toml.get(section), &mut crates);
    }

    if let Some(targets) = cargo_toml.get("target").and_then(toml::Value::as_table) {
        for target in targets.values() {
            for section in ["dependencies", "dev-dependencies", "build-dependencies"] {
                collect_rust_dependency_keys(target.get(section), &mut crates);
            }
        }
    }

    crates
}

fn load_rust_self_crate_name(root_path: &Path) -> Option<String> {
    let contents = std::fs::read_to_string(root_path.join("Cargo.toml")).ok()?;
    let cargo_toml = toml::from_str::<toml::Table>(&contents).ok()?;
    cargo_toml
        .get("package")
        .and_then(|package| package.get("name"))
        .and_then(toml::Value::as_str)
        .map(normalize_rust_crate_name)
        .filter(|name| !name.is_empty())
}

fn collect_rust_dependency_keys(value: Option<&toml::Value>, crates: &mut HashSet<String>) {
    let Some(table) = value.and_then(toml::Value::as_table) else {
        return;
    };
    for name in table.keys() {
        let name = normalize_rust_crate_name(name);
        if !name.is_empty() {
            crates.insert(name);
        }
    }
}

fn normalize_rust_crate_name(name: &str) -> String {
    name.trim().replace('-', "_")
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
        for type_name in csharp_declared_types(&contents) {
            roots.insert(type_name);
        }
    }
    roots
}

fn build_php_local_symbol_index(candidate_files: &[PathBuf]) -> HashSet<String> {
    let mut symbols = HashSet::new();
    for path in candidate_files {
        let ext = path
            .extension()
            .and_then(|ext| ext.to_str())
            .unwrap_or_default();
        if ext != "php" {
            continue;
        }
        let Ok(contents) = std::fs::read_to_string(path) else {
            continue;
        };
        let namespace = contents.lines().find_map(|line| {
            let line = line.trim();
            line.strip_prefix("namespace ")
                .map(|rest| rest.trim().trim_end_matches([';', '{']).to_string())
        });
        for name in php_declared_symbols(&contents) {
            symbols.insert(name.clone());
            if let Some(namespace) = namespace.as_deref()
                && !namespace.is_empty()
            {
                symbols.insert(format!("{namespace}\\{name}"));
            }
        }
    }
    symbols
}

fn build_ruby_local_constant_roots(candidate_files: &[PathBuf]) -> HashSet<String> {
    let mut roots = HashSet::new();
    for path in candidate_files {
        let ext = path
            .extension()
            .and_then(|ext| ext.to_str())
            .unwrap_or_default();
        if !matches!(ext, "rb" | "rake" | "gemspec") {
            continue;
        }
        let Ok(contents) = std::fs::read_to_string(path) else {
            continue;
        };
        for line in contents.lines() {
            let line = line.trim_start();
            let Some(rest) = line
                .strip_prefix("class ")
                .or_else(|| line.strip_prefix("module "))
            else {
                continue;
            };
            let name = rest
                .split(|ch: char| ch.is_whitespace() || matches!(ch, '<' | '(' | ';' | '#'))
                .next()
                .unwrap_or_default()
                .trim_start_matches("::");
            if let Some(root) = name.split("::").next()
                && is_ruby_constant_name(root)
            {
                roots.insert(root.to_string());
            }
        }
    }
    roots
}

fn load_dart_external_packages(root_path: &Path) -> HashSet<String> {
    let contents = std::fs::read_to_string(root_path.join("pubspec.yaml")).unwrap_or_default();
    let Ok(yaml) = serde_yaml::from_str::<serde_yaml::Value>(&contents) else {
        return HashSet::new();
    };

    let mut packages = HashSet::new();
    for field in ["dependencies", "dev_dependencies", "dependency_overrides"] {
        if let Some(map) = yaml.get(field).and_then(|value| value.as_mapping()) {
            for key in map.keys().filter_map(|key| key.as_str()) {
                if !key.is_empty() && key != "sdk" {
                    packages.insert(key.to_string());
                }
            }
        }
    }
    packages
}

fn load_dart_self_package_name(root_path: &Path) -> Option<String> {
    let contents = std::fs::read_to_string(root_path.join("pubspec.yaml")).ok()?;
    let yaml = serde_yaml::from_str::<serde_yaml::Value>(&contents).ok()?;
    yaml.get("name")
        .and_then(|value| value.as_str())
        .map(ToOwned::to_owned)
}

fn build_elixir_local_module_roots(candidate_files: &[PathBuf]) -> HashSet<String> {
    let mut roots = HashSet::new();
    for path in candidate_files {
        let ext = path
            .extension()
            .and_then(|ext| ext.to_str())
            .unwrap_or_default();
        if !matches!(ext, "ex" | "exs") {
            continue;
        }
        let Ok(contents) = std::fs::read_to_string(path) else {
            continue;
        };
        for line in contents.lines() {
            let line = line.trim_start();
            let Some(rest) = line.strip_prefix("defmodule ") else {
                continue;
            };
            let module = rest
                .split(|ch: char| ch.is_whitespace() || matches!(ch, ',' | '(' | '['))
                .next()
                .unwrap_or_default();
            if let Some(root) = module.split('.').next()
                && is_elixir_alias(root)
            {
                roots.insert(root.to_string());
            }
        }
    }
    roots
}

fn load_elixir_external_roots(root_path: &Path) -> HashMap<String, String> {
    let deps = load_elixir_dependency_names(root_path);
    let mut roots = HashMap::new();
    for dep in deps {
        for root in elixir_dependency_roots(&dep) {
            roots.insert(root.to_string(), root.to_string());
        }
    }
    roots
}

fn load_elixir_dependency_names(root_path: &Path) -> HashSet<String> {
    let mut deps = HashSet::new();
    if let Ok(contents) = std::fs::read_to_string(root_path.join("mix.exs")) {
        for line in contents.lines() {
            let trimmed = line.trim();
            if let Some(rest) = trimmed.strip_prefix("{:") {
                let dep = rest
                    .split(|ch: char| !(ch.is_ascii_alphanumeric() || ch == '_'))
                    .next()
                    .unwrap_or_default();
                if !dep.is_empty() {
                    deps.insert(dep.to_string());
                }
            }
        }
    }
    if let Ok(contents) = std::fs::read_to_string(root_path.join("mix.lock")) {
        for line in contents
            .lines()
            .map(str::trim)
            .filter(|line| !line.is_empty())
        {
            let Some(start) = line.find('"') else {
                continue;
            };
            let rest = &line[start + 1..];
            let Some(end) = rest.find('"') else {
                continue;
            };
            let dep = &rest[..end];
            if dep
                .chars()
                .all(|ch| ch.is_ascii_alphanumeric() || ch == '_')
            {
                deps.insert(dep.to_string());
            }
        }
    }
    deps
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
    for item in split_top_level(group, ',') {
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
    let (is_function, rest) = rest
        .strip_prefix("function ")
        .map(|target| (true, target.trim()))
        .unwrap_or((false, rest.trim()));

    if rest.contains('{') || rest.contains('}') || rest.contains('*') {
        extracted.imports.push(ImportRelation {
            file_path: rel_path.to_string(),
            module_name: rest.to_string(),
        });
        return;
    }

    for item in split_top_level(rest, ',') {
        let item = item.trim();
        if item.is_empty() {
            continue;
        }
        let (target, alias) = split_alias(item);
        let target = target.trim_start_matches('\\');
        extracted.imports.push(ImportRelation {
            file_path: rel_path.to_string(),
            module_name: target.to_string(),
        });
        if !is_external_php_symbol(target, import_context) {
            continue;
        }

        let imported_name = target.rsplit('\\').next().unwrap_or(target);
        let local_alias = alias.unwrap_or(imported_name);
        if is_function {
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

    let module = rest
        .split_whitespace()
        .next()
        .unwrap_or_default()
        .split('.')
        .next()
        .unwrap_or_default();
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
    let Some(root) = ruby_require_root(&required) else {
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
    let Some(module) = import_context.elixir_external_roots.get(root) else {
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
            module: module.clone(),
            module_from_qualifier: true,
        },
    );
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
    let module = module.trim_end_matches('/');
    let last_segment = module.rsplit('/').next().unwrap_or(module);
    last_segment
        .split_once(".v")
        .map(|(name, _)| name)
        .unwrap_or(last_segment)
        .replace('-', "_")
}

fn split_alias(text: &str) -> (&str, Option<&str>) {
    if let Some((name, alias)) = text.split_once(" as ") {
        (name.trim(), Some(alias.trim()))
    } else {
        (text.trim(), None)
    }
}

fn split_rust_use_group(text: &str) -> Option<(&str, &str)> {
    let mut depth = 0usize;
    let mut start = None;

    for (idx, ch) in text.char_indices() {
        match ch {
            '{' => {
                if depth == 0 {
                    start = Some(idx);
                }
                depth += 1;
            }
            '}' if depth > 0 => {
                depth -= 1;
                if depth == 0 {
                    let start = start?;
                    if text[idx + ch.len_utf8()..].trim().is_empty() {
                        return Some((text[..start].trim(), text[start + 1..idx].trim()));
                    }
                    return None;
                }
            }
            _ => {}
        }
    }

    None
}

fn rust_join_use_path(prefix: &str, item: &str) -> Option<String> {
    let prefix = prefix.trim().trim_end_matches("::").trim();
    let item = item.trim();
    if item.is_empty() {
        return None;
    }

    let (item_path, alias) = split_alias(item);
    let item_path = item_path.trim();
    if item_path.is_empty() {
        return None;
    }

    let path = if item_path == "self" {
        if prefix.is_empty() {
            return None;
        }
        prefix.to_string()
    } else if prefix.is_empty() {
        item_path.to_string()
    } else {
        format!("{prefix}::{item_path}")
    };

    Some(match alias {
        Some(alias) if !alias.is_empty() => format!("{path} as {alias}"),
        _ => path,
    })
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
    declared_types(contents, &["class", "interface", "enum", "record"])
}

fn csharp_declared_types(contents: &str) -> Vec<String> {
    declared_types(
        contents,
        &["class", "interface", "enum", "record", "struct"],
    )
}

fn declared_types(contents: &str, keywords: &[&str]) -> Vec<String> {
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

fn php_declared_symbols(contents: &str) -> Vec<String> {
    let mut names = Vec::new();
    let tokens: Vec<&str> = contents
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

fn is_external_php_symbol(path: &str, import_context: &ImportResolutionContext) -> bool {
    !import_context.php_local_symbols.contains(path)
        && path
            .rsplit('\\')
            .next()
            .is_none_or(|name| !import_context.php_local_symbols.contains(name))
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

fn ruby_require_root(required: &str) -> Option<&'static str> {
    match required {
        "json" => Some("JSON"),
        "fileutils" => Some("FileUtils"),
        "net/http" | "net/https" => Some("Net"),
        "faraday" => Some("Faraday"),
        "nokogiri" => Some("Nokogiri"),
        "rspec" | "rspec/expectations" | "rspec/core" | "rspec/mocks" => Some("RSpec"),
        _ => None,
    }
}

fn is_ruby_constant_name(name: &str) -> bool {
    name.chars()
        .next()
        .is_some_and(|ch| ch.is_ascii_uppercase())
        && name
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || ch == '_')
}

fn dart_import_alias(text: &str) -> Option<String> {
    let after_as = text.split_once(" as ")?.1;
    let alias = after_as
        .split_whitespace()
        .next()
        .unwrap_or_default()
        .trim_end_matches(';');
    if alias.is_empty() {
        None
    } else {
        Some(alias.to_string())
    }
}

fn is_external_dart_uri(uri: &str, import_context: &ImportResolutionContext) -> bool {
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

fn elixir_dependency_roots(dep: &str) -> &'static [&'static str] {
    match dep {
        "jason" => &["Jason"],
        "httpoison" => &["HTTPoison"],
        "tesla" => &["Tesla"],
        "req" => &["Req"],
        "finch" => &["Finch"],
        "mint" => &["Mint"],
        "ecto" => &["Ecto"],
        "phoenix" => &["Phoenix"],
        "plug" => &["Plug"],
        "oban" => &["Oban"],
        "broadway" => &["Broadway"],
        "nimble_options" => &["NimbleOptions"],
        "nimble_parsec" => &["NimbleParsec"],
        "telemetry" => &["Telemetry"],
        "benchee" => &["Benchee"],
        "ex_doc" => &["ExDoc"],
        _ => &[],
    }
}

fn is_elixir_alias(name: &str) -> bool {
    name.chars()
        .next()
        .is_some_and(|ch| ch.is_ascii_uppercase())
        && name
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || ch == '_')
}

fn is_elixir_alias_path(path: &str) -> bool {
    path.split('.').all(is_elixir_alias)
}

fn elixir_alias_as(text: &str) -> Option<String> {
    let after = text.split_once(" as: ")?.1;
    let alias = after
        .split([',', ' ', ')', ']'])
        .next()
        .unwrap_or_default()
        .trim();
    if is_elixir_alias(alias) {
        Some(alias.to_string())
    } else {
        None
    }
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

#[cfg(test)]
mod tests {
    use std::fs;

    use tempfile::TempDir;

    use super::*;

    #[test]
    fn loads_rust_inline_table_dependency_names() {
        let tempdir = TempDir::new().expect("tempdir");
        fs::write(
            tempdir.path().join("Cargo.toml"),
            r#"
[package]
name = "app"

[dependencies]
serde = { version = "1.0" }
"tokio-util" = { version = "0.7", features = ["codec"] }
"#,
        )
        .expect("cargo toml");

        let crates = load_rust_external_crates(tempdir.path());

        assert!(crates.contains("serde"));
        assert!(crates.contains("tokio_util"));
    }

    #[test]
    fn loads_rust_dependency_names_from_real_toml_tables() {
        let tempdir = TempDir::new().expect("tempdir");
        fs::write(
            tempdir.path().join("Cargo.toml"),
            r#"
[package]
name = "app"

[dependencies]
serde = "1" # keep comment parsing delegated to TOML

[dev-dependencies]
pretty-assertions = "1"

[build-dependencies]
bindgen = "0.69"

[target.'cfg(unix)'.dependencies]
nix = "0.27"

[target.x86_64-pc-windows-msvc.dev-dependencies]
windows-sys = "0.52"

[target.'cfg(target_os = "linux")'.build-dependencies]
cc = "1"
"#,
        )
        .expect("cargo toml");

        let crates = load_rust_external_crates(tempdir.path());

        for name in [
            "serde",
            "pretty_assertions",
            "bindgen",
            "nix",
            "windows_sys",
            "cc",
        ] {
            assert!(crates.contains(name), "missing {name}");
        }
    }

    #[test]
    fn ignores_rust_non_dependency_toml_tables() {
        let tempdir = TempDir::new().expect("tempdir");
        fs::write(
            tempdir.path().join("Cargo.toml"),
            r#"
[package]
name = "app"

[workspace.dependencies]
workspace-only = "1"

[package.metadata.dependencies]
metadata-only = "1"

[features]
serde = []
"#,
        )
        .expect("cargo toml");

        let crates = load_rust_external_crates(tempdir.path());

        assert!(!crates.contains("workspace_only"));
        assert!(!crates.contains("metadata_only"));
        assert!(!crates.contains("serde"));
    }

    #[test]
    fn normalizes_rust_package_name_from_cargo_toml() {
        let tempdir = TempDir::new().expect("tempdir");
        fs::write(
            tempdir.path().join("Cargo.toml"),
            r#"
[package]
name = "my-crate"
"#,
        )
        .expect("cargo toml");

        assert_eq!(
            load_rust_self_crate_name(tempdir.path()).as_deref(),
            Some("my_crate")
        );
    }

    #[test]
    fn rust_grouped_imports_register_named_bare_bindings() {
        let mut extracted = ExtractedImports::default();

        parse_import_statement(
            "rust",
            "use std::collections::{HashMap, HashSet as Set};",
            "src/lib.rs",
            &ImportResolutionContext::default(),
            &mut extracted,
        );

        let hash_map = extracted
            .bindings
            .bare
            .get("HashMap")
            .expect("HashMap binding");
        assert_eq!(hash_map.module, "std::collections");
        assert_eq!(hash_map.callee_name, "HashMap");
        let set = extracted.bindings.bare.get("Set").expect("Set binding");
        assert_eq!(set.module, "std::collections");
        assert_eq!(set.callee_name, "HashSet");
        assert_eq!(
            extracted.bindings.member.get("Set").map(String::as_str),
            Some("std::collections::HashSet")
        );
        assert!(extracted.bindings.external_roots.contains_key("std"));
    }

    #[test]
    fn rust_glob_imports_do_not_register_individual_bare_bindings() {
        let mut extracted = ExtractedImports::default();

        parse_import_statement(
            "rust",
            "use std::collections::*;",
            "src/lib.rs",
            &ImportResolutionContext::default(),
            &mut extracted,
        );

        assert!(extracted.bindings.bare.is_empty());
        assert!(extracted.bindings.member.is_empty());
    }

    #[test]
    fn loads_elixir_mix_lock_first_quoted_dependency_per_line() {
        let tempdir = TempDir::new().expect("tempdir");
        fs::write(
            tempdir.path().join("mix.lock"),
            r#"%{
  "jason": {:hex, :jason, "1.4.4", "checksum", [:mix], [], "hexpm", "repo"},
  "httpoison": {:hex, :httpoison, "2.2.1", "checksum", [:mix], [], "hexpm", "repo"}
}"#,
        )
        .expect("mix.lock");

        let deps = load_elixir_dependency_names(tempdir.path());

        assert!(deps.contains("jason"));
        assert!(deps.contains("httpoison"));
        assert!(!deps.contains("1"));
        assert!(!deps.contains("hexpm"));
    }

    #[test]
    fn go_default_package_alias_uses_last_segment_before_version_suffix() {
        assert_eq!(go_default_package_alias("gopkg.in/yaml.v3"), "yaml");
        assert_eq!(
            go_default_package_alias("github.com/acme/api-client/"),
            "api_client"
        );
    }

    #[test]
    fn csharp_declared_types_includes_structs() {
        let names = csharp_declared_types(
            "public struct Point {} class Sample {} interface IThing {} enum Mode {} record Data;",
        );

        assert!(names.iter().any(|name| name == "Point"));
        assert!(names.iter().any(|name| name == "Sample"));
        assert!(names.iter().any(|name| name == "IThing"));
        assert!(names.iter().any(|name| name == "Mode"));
        assert!(names.iter().any(|name| name == "Data"));
    }

    #[test]
    fn empty_php_fully_qualified_namespace_stays_unresolved() {
        let target = resolve_external_callee(
            &ImportBindings::default(),
            &[],
            "helper",
            Some(""),
            Some("\\"),
            false,
        );

        assert!(target.is_none());
    }
}
