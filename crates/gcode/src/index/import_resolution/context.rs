use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

use crate::models::ImportRelation;

use super::helpers::{is_elixir_alias, is_ruby_constant_name};
use super::predicates::{
    csharp_declared_types, elixir_dependency_roots, java_declared_types, php_declared_symbols,
    ruby_require_root,
};

#[derive(Debug, Clone, Default)]
pub struct ImportResolutionContext {
    pub(super) python_modules: HashSet<String>,
    pub(super) js_external_packages: HashSet<String>,
    pub(super) js_self_package_name: Option<String>,
    pub(super) go_module_path: Option<String>,
    pub(super) rust_external_crates: HashSet<String>,
    pub(super) rust_self_crate_name: Option<String>,
    pub(super) java_local_classes: HashSet<String>,
    pub(super) csharp_local_roots: HashSet<String>,
    pub(super) php_local_symbols: HashSet<String>,
    pub(super) ruby_local_constant_roots: HashSet<String>,
    pub(super) ruby_require_root_overrides: HashMap<String, String>,
    pub(super) dart_external_packages: HashSet<String>,
    pub(super) dart_self_package_name: Option<String>,
    pub(super) elixir_external_roots: HashMap<String, String>,
    pub(super) elixir_external_root_overrides: HashMap<String, String>,
    pub(super) elixir_local_module_roots: HashSet<String>,
}

impl ImportResolutionContext {
    pub(super) fn ruby_require_root(&self, required: &str) -> Option<&str> {
        self.ruby_require_root_overrides
            .get(required)
            .map(String::as_str)
            .or_else(|| ruby_require_root(required))
    }

    pub(super) fn elixir_external_root_module(&self, root: &str) -> Option<&str> {
        self.elixir_external_root_overrides
            .get(root)
            .or_else(|| self.elixir_external_roots.get(root))
            .map(String::as_str)
    }
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

pub(super) const JS_BUILTIN_MODULES: &[&str] = &[
    "assert",
    "assert/strict",
    "async_hooks",
    "buffer",
    "child_process",
    "cluster",
    "console",
    "constants",
    "crypto",
    "dgram",
    "diagnostics_channel",
    "dns",
    "dns/promises",
    "domain",
    "events",
    "fs",
    "fs/promises",
    "http",
    "http2",
    "https",
    "inspector",
    "inspector/promises",
    "net",
    "module",
    "os",
    "path",
    "path/posix",
    "path/win32",
    "perf_hooks",
    "process",
    "punycode",
    "querystring",
    "readline",
    "readline/promises",
    "repl",
    "sea",
    "stream",
    "stream/consumers",
    "stream/promises",
    "stream/web",
    "string_decoder",
    "sqlite",
    "sys",
    "timers",
    "timers/promises",
    "test",
    "test/reporters",
    "tls",
    "trace_events",
    "tty",
    "url",
    "util",
    "v8",
    "vm",
    "wasi",
    "worker_threads",
    "zlib",
];

pub fn build_import_resolution_context(
    root_path: &Path,
    candidate_files: &[PathBuf],
) -> ImportResolutionContext {
    build_import_resolution_context_with_overrides(
        root_path,
        candidate_files,
        HashMap::new(),
        HashMap::new(),
    )
}

pub fn build_import_resolution_context_with_overrides(
    root_path: &Path,
    candidate_files: &[PathBuf],
    ruby_require_root_overrides: HashMap<String, String>,
    elixir_external_root_overrides: HashMap<String, String>,
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
        ruby_require_root_overrides,
        dart_external_packages: load_dart_external_packages(root_path),
        dart_self_package_name: load_dart_self_package_name(root_path),
        elixir_external_roots: load_elixir_external_roots(root_path),
        elixir_external_root_overrides,
        elixir_local_module_roots: build_elixir_local_module_roots(candidate_files),
    }
}
pub(super) fn build_python_module_index(
    root_path: &Path,
    candidate_files: &[PathBuf],
) -> HashSet<String> {
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

pub(super) fn load_js_external_packages(root_path: &Path) -> HashSet<String> {
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

pub(super) fn load_js_self_package_name(root_path: &Path) -> Option<String> {
    let package_json = root_path.join("package.json");
    let contents = std::fs::read_to_string(package_json).ok()?;
    let json = serde_json::from_str::<serde_json::Value>(&contents).ok()?;
    json.get("name")
        .and_then(|value| value.as_str())
        .map(ToOwned::to_owned)
}

pub(super) fn load_go_module_path(root_path: &Path) -> Option<String> {
    let contents = std::fs::read_to_string(root_path.join("go.mod")).ok()?;
    contents.lines().find_map(|line| {
        let line = line.trim();
        line.strip_prefix("module ")
            .map(str::trim)
            .filter(|module| !module.is_empty())
            .map(ToOwned::to_owned)
    })
}

pub(super) fn load_rust_external_crates(root_path: &Path) -> HashSet<String> {
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

pub(super) fn load_rust_self_crate_name(root_path: &Path) -> Option<String> {
    let contents = std::fs::read_to_string(root_path.join("Cargo.toml")).ok()?;
    let cargo_toml = toml::from_str::<toml::Table>(&contents).ok()?;
    cargo_toml
        .get("package")
        .and_then(|package| package.get("name"))
        .and_then(toml::Value::as_str)
        .map(normalize_rust_crate_name)
        .filter(|name| !name.is_empty())
}

pub(super) fn collect_rust_dependency_keys(
    value: Option<&toml::Value>,
    crates: &mut HashSet<String>,
) {
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

pub(super) fn normalize_rust_crate_name(name: &str) -> String {
    name.trim().replace('-', "_")
}

pub(super) fn build_java_local_class_index(candidate_files: &[PathBuf]) -> HashSet<String> {
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

pub(super) fn build_csharp_local_roots(candidate_files: &[PathBuf]) -> HashSet<String> {
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

pub(super) fn build_php_local_symbol_index(candidate_files: &[PathBuf]) -> HashSet<String> {
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

pub(super) fn build_ruby_local_constant_roots(candidate_files: &[PathBuf]) -> HashSet<String> {
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

pub(super) fn load_dart_external_packages(root_path: &Path) -> HashSet<String> {
    let Ok(contents) = std::fs::read_to_string(root_path.join("pubspec.yaml")) else {
        return HashSet::new();
    };
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

pub(super) fn load_dart_self_package_name(root_path: &Path) -> Option<String> {
    let contents = std::fs::read_to_string(root_path.join("pubspec.yaml")).ok()?;
    let yaml = serde_yaml::from_str::<serde_yaml::Value>(&contents).ok()?;
    yaml.get("name")
        .and_then(|value| value.as_str())
        .map(ToOwned::to_owned)
}

pub(super) fn build_elixir_local_module_roots(candidate_files: &[PathBuf]) -> HashSet<String> {
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

pub(super) fn load_elixir_external_roots(root_path: &Path) -> HashMap<String, String> {
    let deps = load_elixir_dependency_names(root_path);
    let mut roots = HashMap::new();
    for dep in deps {
        if let Some(dep_roots) = elixir_dependency_roots(&dep) {
            for root in dep_roots {
                roots.insert(root.clone(), root.clone());
            }
        }
    }
    roots
}

pub(super) fn load_elixir_dependency_names(root_path: &Path) -> HashSet<String> {
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
