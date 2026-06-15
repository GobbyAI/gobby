use std::collections::{HashMap, HashSet};
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use rayon::prelude::*;
use regex::Regex;

use crate::models::ImportRelation;

use super::helpers::{is_elixir_alias, is_ruby_constant_name};
use super::js_local::js_candidate_files;
use super::predicates::{
    csharp_declared_types, elixir_dependency_roots, java_declared_types, php_declared_symbols,
    ruby_require_root,
};
use super::rust_local::{rust_candidate_files, rust_import_target, rust_qualified_call_target};

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
    pub(super) swift_local_modules: HashSet<String>,
    pub(super) dart_external_packages: HashSet<String>,
    pub(super) dart_self_package_name: Option<String>,
    pub(super) elixir_external_roots: HashMap<String, String>,
    pub(super) elixir_external_root_overrides: HashMap<String, String>,
    pub(super) elixir_local_module_roots: HashSet<String>,
}

impl ImportResolutionContext {
    /// Candidate target files for a JavaScript/TypeScript import `specifier`
    /// resolved relative to `rel_path`, derived by pure path logic (no file
    /// reads). The post-write pass narrows these against the indexed symbols.
    pub(super) fn js_candidate_files(&self, rel_path: &str, specifier: &str) -> Vec<String> {
        js_candidate_files(rel_path, self.js_self_package_name.as_deref(), specifier)
    }

    /// Local-call binding for a Rust `use` path import (e.g. `use crate::m::f`),
    /// resolved to candidate module files without reading them. `None` when the
    /// path does not name a local module.
    pub(super) fn rust_import_candidate(
        &self,
        rel_path: &str,
        path: &str,
    ) -> Option<LocalCallBinding> {
        let target = rust_import_target(rel_path, self.rust_self_crate_name.as_deref(), path)?;
        Some(LocalCallBinding {
            candidate_files: rust_candidate_files(&target.source_root, &target.module),
            callee_name: target.name,
        })
    }

    /// Local-call binding for a path-qualified Rust call without a `use`
    /// (e.g. `crate::m::f()`), resolved against the module tree by path logic.
    pub(super) fn rust_qualified_candidate(
        &self,
        rel_path: &str,
        qualifier_path: &str,
        name: &str,
    ) -> Option<LocalCallBinding> {
        let target = rust_qualified_call_target(
            rel_path,
            self.rust_self_crate_name.as_deref(),
            qualifier_path,
            name,
        )?;
        Some(LocalCallBinding {
            candidate_files: rust_candidate_files(&target.source_root, &target.module),
            callee_name: target.name,
        })
    }

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

/// A cross-file local import resolved at parse time to its candidate target
/// files plus the originally imported name. Resolution to a canonical symbol id
/// happens later, against `code_symbols`, in the post-write pass — so this type
/// carries no file coordinates and never recomputes a UUID.
#[derive(Debug, Clone)]
pub(crate) struct LocalCallBinding {
    /// Project-relative files the target might live in, derived from the import
    /// by pure path logic. Narrowed against indexed symbols post-write.
    pub(crate) candidate_files: Vec<String>,
    /// The originally imported name (not the local alias).
    pub(crate) callee_name: String,
}

#[derive(Debug, Clone, Default)]
pub(crate) struct ImportBindings {
    pub(crate) bare: HashMap<String, ExternalImportBinding>,
    pub(crate) local_bare: HashMap<String, LocalCallBinding>,
    /// Namespace alias (`import * as ns`) -> candidate files of the imported
    /// module. A member call `ns.fn()` resolves `fn` against these files.
    pub(crate) local_member: HashMap<String, Vec<String>>,
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

// Curated from the Node.js built-in module list in the official Node API docs,
// checked 2026-06-01. Keep this explicit so import resolution stays offline.
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
    "stream/iter",
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
    "util/types",
    "v8",
    "vm",
    "wasi",
    "worker_threads",
    "zlib",
    "zlib/iter",
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
        swift_local_modules: build_swift_local_modules(root_path, candidate_files),
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
        modules.extend(python_module_names_for_path(root_path, path));
    }

    modules
}

/// Project-relative files a Python dotted `module` could be defined in, derived
/// by inverting [`python_module_names_for_path`] — no file reads. Covers both
/// `pkg/mod.py` and the package form `pkg/mod/__init__.py`, the `.pyi` stub
/// variants, and a `src/`-layout prefix. The post-write pass narrows these
/// against the actually-indexed symbols, so listing non-existent paths is safe.
pub(super) fn python_candidate_files(module: &str) -> Vec<String> {
    let base = module.replace('.', "/");
    let mut files = Vec::with_capacity(8);
    for stem in [&base, &format!("src/{base}")] {
        for ext in ["py", "pyi"] {
            files.push(format!("{stem}.{ext}"));
            files.push(format!("{stem}/__init__.{ext}"));
        }
    }
    files
}

fn python_module_names_for_path(root_path: &Path, path: &Path) -> Vec<String> {
    let Ok(rel) = path.strip_prefix(root_path) else {
        return Vec::new();
    };
    let ext = rel
        .extension()
        .and_then(|ext| ext.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    if !matches!(ext.as_str(), "py" | "pyi") {
        return Vec::new();
    }

    let mut module = rel
        .with_extension("")
        .to_string_lossy()
        .replace(['/', '\\'], ".");
    if module.ends_with(".__init__") {
        module.truncate(module.len() - ".__init__".len());
    }
    if module.is_empty() {
        return Vec::new();
    }

    let mut modules = vec![module.clone()];
    if let Some(stripped) = module.strip_prefix("src.") {
        modules.push(stripped.to_string());
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
        "bundleDependencies",
    ] {
        let Some(value) = json.get(field) else {
            continue;
        };
        if let Some(map) = value.as_object() {
            packages.extend(map.keys().cloned());
        } else if let Some(array) = value.as_array() {
            packages.extend(
                array
                    .iter()
                    .filter_map(|value| value.as_str().map(str::to_owned)),
            );
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
    let mut crates = HashSet::new();
    for manifest in rust_manifest_paths(root_path) {
        let Ok(contents) = std::fs::read_to_string(manifest) else {
            continue;
        };
        let Ok(cargo_toml) = toml::from_str::<toml::Table>(&contents) else {
            continue;
        };

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
    }

    crates
}

fn rust_manifest_paths(root_path: &Path) -> Vec<PathBuf> {
    let root_manifest = root_path.join("Cargo.toml");
    let mut manifests = vec![root_manifest.clone()];
    let Ok(contents) = std::fs::read_to_string(&root_manifest) else {
        return manifests;
    };
    let Ok(cargo_toml) = toml::from_str::<toml::Table>(&contents) else {
        return manifests;
    };
    let Some(members) = cargo_toml
        .get("workspace")
        .and_then(|workspace| workspace.get("members"))
        .and_then(toml::Value::as_array)
    else {
        return manifests;
    };
    for member in members.iter().filter_map(toml::Value::as_str) {
        if member.contains('*') {
            let pattern = root_path.join(member).join("Cargo.toml");
            let Some(pattern) = pattern.to_str() else {
                continue;
            };
            let Ok(entries) = glob::glob(pattern) else {
                log::debug!(
                    "invalid Cargo workspace glob member `{member}` under {}",
                    root_path.display()
                );
                continue;
            };
            manifests.extend(entries.flatten().filter(|path| path.is_file()));
            continue;
        }
        let manifest = root_path.join(member).join("Cargo.toml");
        if manifest.is_file() {
            manifests.push(manifest);
        }
    }
    manifests.sort();
    manifests.dedup();
    manifests
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
    candidate_files
        .par_iter()
        .map(|path| {
            let mut classes = HashSet::new();
            let ext = path
                .extension()
                .and_then(|ext| ext.to_str())
                .unwrap_or_default();
            if ext != "java" {
                return classes;
            }
            let Ok(file) = File::open(path) else {
                return classes;
            };
            let mut package = None;
            for line in BufReader::new(file).lines().map_while(Result::ok) {
                let line = line.trim();
                if package.is_none() {
                    package = line
                        .strip_prefix("package ")
                        .map(|rest| rest.trim().trim_end_matches(';').trim().to_string());
                }
                for class_name in java_declared_types(line) {
                    classes.insert(class_name.clone());
                    if let Some(package) = package.as_deref()
                        && !package.is_empty()
                    {
                        classes.insert(format!("{package}.{class_name}"));
                    }
                }
            }
            classes
        })
        .reduce(HashSet::new, |mut all, classes| {
            all.extend(classes);
            all
        })
}

pub(super) fn build_csharp_local_roots(candidate_files: &[PathBuf]) -> HashSet<String> {
    candidate_files
        .par_iter()
        .map(|path| {
            let mut roots = HashSet::new();
            let ext = path
                .extension()
                .and_then(|ext| ext.to_str())
                .unwrap_or_default();
            if ext != "cs" {
                return roots;
            }
            let Ok(file) = File::open(path) else {
                return roots;
            };
            for line in BufReader::new(file).lines().map_while(Result::ok) {
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
                for type_name in csharp_declared_types(line) {
                    roots.insert(type_name);
                }
            }
            roots
        })
        .reduce(HashSet::new, |mut all, roots| {
            all.extend(roots);
            all
        })
}

pub(super) fn build_php_local_symbol_index(candidate_files: &[PathBuf]) -> HashSet<String> {
    candidate_files
        .par_iter()
        .map(|path| {
            let mut symbols = HashSet::new();
            let ext = path
                .extension()
                .and_then(|ext| ext.to_str())
                .unwrap_or_default();
            if ext != "php" {
                return symbols;
            }
            let Ok(file) = File::open(path) else {
                return symbols;
            };
            let mut namespace = None;
            for line in BufReader::new(file).lines().map_while(Result::ok) {
                let line = line.trim();
                if namespace.is_none() {
                    namespace = line
                        .strip_prefix("namespace ")
                        .map(|rest| rest.trim().trim_end_matches([';', '{']).to_string());
                }
                for name in php_declared_symbols(line) {
                    symbols.insert(name.to_ascii_lowercase());
                    if let Some(namespace) = namespace.as_deref()
                        && !namespace.is_empty()
                    {
                        let qualified = format!("{namespace}\\{name}");
                        symbols.insert(qualified.to_ascii_lowercase());
                    }
                }
            }
            symbols
        })
        .reduce(HashSet::new, |mut all, symbols| {
            all.extend(symbols);
            all
        })
}

pub(super) fn build_ruby_local_constant_roots(candidate_files: &[PathBuf]) -> HashSet<String> {
    candidate_files
        .par_iter()
        .map(|path| {
            let mut roots = HashSet::new();
            let ext = path
                .extension()
                .and_then(|ext| ext.to_str())
                .unwrap_or_default();
            if !matches!(ext, "rb" | "rake" | "gemspec") {
                return roots;
            }
            let Ok(file) = File::open(path) else {
                return roots;
            };
            for line in BufReader::new(file).lines().map_while(Result::ok) {
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
            roots
        })
        .reduce(HashSet::new, |mut all, roots| {
            all.extend(roots);
            all
        })
}

pub(super) fn build_swift_local_modules(
    root_path: &Path,
    candidate_files: &[PathBuf],
) -> HashSet<String> {
    candidate_files
        .par_iter()
        .map(|path| {
            let mut modules = HashSet::new();
            let ext = path
                .extension()
                .and_then(|ext| ext.to_str())
                .unwrap_or_default();
            if ext != "swift" {
                return modules;
            }
            let rel = path.strip_prefix(root_path).unwrap_or(path.as_path());
            let components = rel
                .components()
                .filter_map(|component| component.as_os_str().to_str())
                .collect::<Vec<_>>();
            for window in components.windows(2) {
                if matches!(window[0], "Sources" | "Tests") && !window[1].is_empty() {
                    modules.insert(window[1].to_string());
                }
            }
            if let Some(parent) = rel
                .parent()
                .and_then(Path::file_name)
                .and_then(|name| name.to_str())
                && !parent.is_empty()
                && parent != "Sources"
                && parent != "Tests"
            {
                modules.insert(parent.to_string());
            }
            modules
        })
        .reduce(HashSet::new, |mut all, modules| {
            all.extend(modules);
            all
        })
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
    candidate_files
        .par_iter()
        .map(|path| {
            let mut roots = HashSet::new();
            let ext = path
                .extension()
                .and_then(|ext| ext.to_str())
                .unwrap_or_default();
            if !matches!(ext, "ex" | "exs") {
                return roots;
            }
            let Ok(file) = File::open(path) else {
                return roots;
            };
            for line in BufReader::new(file).lines().map_while(Result::ok) {
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
            roots
        })
        .reduce(HashSet::new, |mut all, roots| {
            all.extend(roots);
            all
        })
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
        // This is a whole-file manifest heuristic, not an Elixir parser. It catches
        // normal deps entries even when tuple formatting spans lines.
        for captures in elixir_mix_dependency_regex().captures_iter(&contents) {
            if let Some(dep) = captures.get(1) {
                deps.insert(dep.as_str().to_string());
            }
        }
    }
    if let Ok(contents) = std::fs::read_to_string(root_path.join("mix.lock")) {
        // Lockfiles are Elixir maps; quoted dependency keys are enough here. Values
        // may contain package names and repository names that should not be indexed.
        for captures in elixir_lock_dependency_regex().captures_iter(&contents) {
            if let Some(dep) = captures.get(1) {
                deps.insert(dep.as_str().to_string());
            }
        }
    }
    deps
}

fn elixir_mix_dependency_regex() -> &'static Regex {
    static REGEX: OnceLock<Regex> = OnceLock::new();
    REGEX.get_or_init(|| {
        Regex::new(r"\{\s*:([A-Za-z_][A-Za-z0-9_]*)\b").expect("Elixir dependency regex compiles")
    })
}

fn elixir_lock_dependency_regex() -> &'static Regex {
    static REGEX: OnceLock<Regex> = OnceLock::new();
    REGEX.get_or_init(|| {
        Regex::new(r#""([A-Za-z_][A-Za-z0-9_]*)"\s*:"#)
            .expect("Elixir lock dependency regex compiles")
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_candidate_files_cover_module_package_and_src_layouts() {
        let files = python_candidate_files("pkg.utils");
        for expected in [
            "pkg/utils.py",
            "pkg/utils/__init__.py",
            "pkg/utils.pyi",
            "src/pkg/utils.py",
        ] {
            assert!(files.iter().any(|file| file == expected), "{files:?}");
        }
    }

    #[test]
    fn python_candidate_files_handle_top_level_module() {
        let files = python_candidate_files("a");
        assert!(files.iter().any(|file| file == "a.py"), "{files:?}");
        assert!(
            files.iter().any(|file| file == "a/__init__.py"),
            "{files:?}"
        );
    }
}
