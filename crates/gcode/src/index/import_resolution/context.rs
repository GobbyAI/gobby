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
    /// Maps a project-relative package directory to the Go source files it
    /// contains. A local selector call `pkg.Fn()` resolves `Fn` against any
    /// file in the imported package's directory (Go packages are
    /// directory-granular).
    pub(super) go_package_files: HashMap<String, Vec<String>>,
    pub(super) rust_external_crates: HashSet<String>,
    pub(super) rust_self_crate_name: Option<String>,
    pub(super) java_local_classes: HashSet<String>,
    /// Maps a locally-declared fully-qualified class name (`pkg.Class`) to the
    /// project-relative files that declare it. A local single-type import
    /// `import pkg.Class;` resolves `Class.m()`/`new Class()` against these
    /// files (Java classes are file-granular per the public-class convention).
    pub(super) java_class_files: HashMap<String, Vec<String>>,
    pub(super) csharp_local_roots: HashSet<String>,
    /// Maps a locally-declared C# type's fully-qualified name (`Ns.Type`) to the
    /// project-relative files declaring it. A member call resolves its type
    /// against these files: a fully-qualified `Ns.Type.M()`, an aliased
    /// `using X = Ns.Type;` then `X.M()`, or a namespace-imported `using Ns;`
    /// then `Type.M()`. The post-write DB pass narrows to the real symbol.
    pub(super) csharp_type_files: HashMap<String, Vec<String>>,
    /// Maps a locally-declared Kotlin package (`com.example`) to the
    /// project-relative files declaring it. A local import `import com.example.X`
    /// resolves `X()`/`X.m()` against these files. Kotlin is package-granular:
    /// file names need not match declared types, and top-level functions live at
    /// package scope, so a name can be in any file of its package. The post-write
    /// DB pass narrows to the real symbol.
    pub(super) kotlin_package_files: HashMap<String, Vec<String>>,
    pub(super) php_local_symbols: HashSet<String>,
    pub(super) ruby_local_constant_roots: HashSet<String>,
    /// Maps a locally-declared Ruby constant root (`Widget`, the first `::`
    /// segment of a `class`/`module` name) to the project-relative files that
    /// declare it. A member call `Widget.build`/`Widget.new` resolves against
    /// these files; the post-write DB pass narrows to the real symbol. Ruby
    /// `require` does not import names, so resolution is constant-driven rather
    /// than import-driven.
    pub(super) ruby_constant_files: HashMap<String, Vec<String>>,
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

    /// Candidate target files for a local Go package `module` (a self-module
    /// import path), found by mapping the import path to its package directory.
    /// Go packages are directory-granular, so this returns every indexed Go
    /// file in that directory; the post-write pass narrows to the real symbol.
    /// Empty when `module` is external or names no indexed package directory.
    pub(super) fn go_candidate_files(&self, module: &str) -> Vec<String> {
        let Some(self_module) = self.go_module_path.as_deref() else {
            return Vec::new();
        };
        let dir = if module == self_module {
            String::new()
        } else if let Some(rest) = module.strip_prefix(&format!("{self_module}/")) {
            rest.to_string()
        } else {
            return Vec::new();
        };
        self.go_package_files.get(&dir).cloned().unwrap_or_default()
    }

    /// Project-relative files declaring the local class named by `fqcn` (a
    /// fully-qualified `pkg.Class`). Empty when no indexed local file declares
    /// it (e.g. an external class, or a local simple-name collision whose
    /// fully-qualified form is not actually declared here).
    pub(super) fn java_candidate_files(&self, fqcn: &str) -> Vec<String> {
        self.java_class_files.get(fqcn).cloned().unwrap_or_default()
    }

    /// Project-relative files declaring the local C# type named by `type_path`
    /// (a fully-qualified `Ns.Type`). Empty when no indexed local file declares
    /// it (e.g. an external type, or a namespace/type pair not actually present
    /// here).
    pub(super) fn csharp_type_files(&self, type_path: &str) -> Vec<String> {
        self.csharp_type_files
            .get(type_path)
            .cloned()
            .unwrap_or_default()
    }

    /// Project-relative files declaring the local Kotlin `package`. Empty when no
    /// indexed local file declares that package (an external import, or an
    /// unknown package — e.g. a `pkg.Type.member` import whose package portion
    /// `pkg.Type` is a type, not a declared package).
    pub(super) fn kotlin_package_files(&self, package: &str) -> Vec<String> {
        self.kotlin_package_files
            .get(package)
            .cloned()
            .unwrap_or_default()
    }

    pub(super) fn ruby_require_root(&self, required: &str) -> Option<&str> {
        self.ruby_require_root_overrides
            .get(required)
            .map(String::as_str)
            .or_else(|| ruby_require_root(required))
    }

    pub(super) fn ruby_constant_files(&self, root: &str) -> Vec<String> {
        self.ruby_constant_files
            .get(root)
            .cloned()
            .unwrap_or_default()
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
    /// Namespaces brought into scope by a local C# `using Ns;`. A simple-type
    /// member call `Type.M()` resolves `Type` against each namespace's declared
    /// types (`csharp_type_files["Ns.Type"]`).
    pub(crate) csharp_local_namespaces: Vec<String>,
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
    let java_index = build_java_class_index(root_path, candidate_files);
    let csharp_index = build_csharp_index(root_path, candidate_files);
    let ruby_constant_files = build_ruby_constant_files(root_path, candidate_files);
    ImportResolutionContext {
        python_modules: build_python_module_index(root_path, candidate_files),
        js_external_packages: load_js_external_packages(root_path),
        js_self_package_name: load_js_self_package_name(root_path),
        go_module_path: load_go_module_path(root_path),
        go_package_files: build_go_package_files(root_path, candidate_files),
        rust_external_crates: load_rust_external_crates(root_path),
        rust_self_crate_name: load_rust_self_crate_name(root_path),
        java_local_classes: java_index.local_classes,
        java_class_files: java_index.class_files,
        csharp_local_roots: csharp_index.local_roots,
        csharp_type_files: csharp_index.type_files,
        kotlin_package_files: build_kotlin_package_files(root_path, candidate_files),
        php_local_symbols: build_php_local_symbol_index(candidate_files),
        ruby_local_constant_roots: ruby_constant_files.keys().cloned().collect(),
        ruby_constant_files,
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

/// Index every discovered Go source file by its project-relative package
/// directory. `go_candidate_files` consults this so a local selector call
/// `pkg.Fn()` can resolve against any file in the imported package directory,
/// matching Go's directory-granular package model.
pub(super) fn build_go_package_files(
    root_path: &Path,
    candidate_files: &[PathBuf],
) -> HashMap<String, Vec<String>> {
    let mut packages: HashMap<String, Vec<String>> = HashMap::new();
    for path in candidate_files {
        let ext = path
            .extension()
            .and_then(|ext| ext.to_str())
            .unwrap_or_default();
        if ext != "go" {
            continue;
        }
        let Ok(rel) = path.strip_prefix(root_path) else {
            continue;
        };
        let rel_str = rel.to_string_lossy().replace('\\', "/");
        let dir = rel
            .parent()
            .map(|parent| parent.to_string_lossy().replace('\\', "/"))
            .unwrap_or_default();
        packages.entry(dir).or_default().push(rel_str);
    }
    for files in packages.values_mut() {
        files.sort();
        files.dedup();
    }
    packages
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

/// Locally-declared Java class names plus the files that declare each one.
pub(super) struct JavaClassIndex {
    /// Simple and fully-qualified class names declared by local files. Used to
    /// classify an import target as local (`is_external_java_class`).
    pub(super) local_classes: HashSet<String>,
    /// Fully-qualified class name (`pkg.Class`) -> declaring project-relative
    /// files. Used to map a local single-type import to its target file(s).
    pub(super) class_files: HashMap<String, Vec<String>>,
}

pub(super) fn build_java_class_index(
    root_path: &Path,
    candidate_files: &[PathBuf],
) -> JavaClassIndex {
    let (local_classes, mut class_files) = candidate_files
        .par_iter()
        .map(|path| {
            let mut local_classes = HashSet::new();
            let mut class_files: HashMap<String, Vec<String>> = HashMap::new();
            let ext = path
                .extension()
                .and_then(|ext| ext.to_str())
                .unwrap_or_default();
            if ext != "java" {
                return (local_classes, class_files);
            }
            let Ok(file) = File::open(path) else {
                return (local_classes, class_files);
            };
            let rel = path.strip_prefix(root_path).unwrap_or(path);
            let rel_str = rel.to_string_lossy().replace('\\', "/");
            let mut package = None;
            for line in BufReader::new(file).lines().map_while(Result::ok) {
                let line = line.trim();
                if package.is_none() {
                    package = line
                        .strip_prefix("package ")
                        .map(|rest| rest.trim().trim_end_matches(';').trim().to_string());
                }
                for class_name in java_declared_types(line) {
                    local_classes.insert(class_name.clone());
                    if let Some(package) = package.as_deref()
                        && !package.is_empty()
                    {
                        let fqcn = format!("{package}.{class_name}");
                        local_classes.insert(fqcn.clone());
                        class_files.entry(fqcn).or_default().push(rel_str.clone());
                    }
                }
            }
            (local_classes, class_files)
        })
        .reduce(
            || (HashSet::new(), HashMap::new()),
            |mut acc, (classes, files)| {
                acc.0.extend(classes);
                for (fqcn, paths) in files {
                    acc.1.entry(fqcn).or_default().extend(paths);
                }
                acc
            },
        );
    for files in class_files.values_mut() {
        files.sort();
        files.dedup();
    }
    JavaClassIndex {
        local_classes,
        class_files,
    }
}

pub(super) struct CsharpIndex {
    /// Namespace roots and simple type names declared by local files. Used to
    /// classify a `using` target as local (`is_external_csharp_path`).
    pub(super) local_roots: HashSet<String>,
    /// Fully-qualified type name (`Ns.Type`) -> declaring project-relative
    /// files. Used to map a local member call to its target file(s).
    pub(super) type_files: HashMap<String, Vec<String>>,
}

pub(super) fn build_csharp_index(root_path: &Path, candidate_files: &[PathBuf]) -> CsharpIndex {
    let (local_roots, mut type_files) = candidate_files
        .par_iter()
        .map(|path| {
            let mut local_roots = HashSet::new();
            let mut type_files: HashMap<String, Vec<String>> = HashMap::new();
            let ext = path
                .extension()
                .and_then(|ext| ext.to_str())
                .unwrap_or_default();
            if ext != "cs" {
                return (local_roots, type_files);
            }
            let Ok(file) = File::open(path) else {
                return (local_roots, type_files);
            };
            let rel = path.strip_prefix(root_path).unwrap_or(path);
            let rel_str = rel.to_string_lossy().replace('\\', "/");
            // Tracks the most recent `namespace` declaration so a declared type
            // is keyed by its fully-qualified name. File-scoped namespaces (the
            // common case) name the whole file; block-scoped namespaces apply to
            // the types that follow them.
            let mut current_namespace: Option<String> = None;
            for line in BufReader::new(file).lines().map_while(Result::ok) {
                let line = line.trim();
                if let Some(rest) = line.strip_prefix("namespace ") {
                    let namespace = rest
                        .trim()
                        .trim_end_matches([';', '{'])
                        .split_whitespace()
                        .next()
                        .unwrap_or_default();
                    if !namespace.is_empty() {
                        if let Some(root) = namespace.split('.').next()
                            && !root.is_empty()
                        {
                            local_roots.insert(root.to_string());
                        }
                        current_namespace = Some(namespace.to_string());
                    }
                }
                for type_name in csharp_declared_types(line) {
                    local_roots.insert(type_name.clone());
                    if let Some(namespace) = current_namespace.as_deref() {
                        let fqcn = format!("{namespace}.{type_name}");
                        type_files.entry(fqcn).or_default().push(rel_str.clone());
                    }
                }
            }
            (local_roots, type_files)
        })
        .reduce(
            || (HashSet::new(), HashMap::new()),
            |mut acc, (roots, files)| {
                acc.0.extend(roots);
                for (fqcn, paths) in files {
                    acc.1.entry(fqcn).or_default().extend(paths);
                }
                acc
            },
        );
    for files in type_files.values_mut() {
        files.sort();
        files.dedup();
    }
    CsharpIndex {
        local_roots,
        type_files,
    }
}

/// Maps each locally-declared Kotlin package to the project-relative files that
/// declare it, by reading every `.kt`/`.kts` file's leading `package`
/// declaration. A file with no `package` line belongs to the root package
/// (empty-string key). Files share a package freely (Kotlin packages are not
/// file-granular), so an import `import pkg.Name` resolves `Name` against any
/// file in `pkg`; the post-write DB pass narrows to the real symbol.
pub(super) fn build_kotlin_package_files(
    root_path: &Path,
    candidate_files: &[PathBuf],
) -> HashMap<String, Vec<String>> {
    let mut package_files = candidate_files
        .par_iter()
        .filter_map(|path| {
            let ext = path
                .extension()
                .and_then(|ext| ext.to_str())
                .unwrap_or_default();
            if ext != "kt" && ext != "kts" {
                return None;
            }
            let file = File::open(path).ok()?;
            let rel = path.strip_prefix(root_path).unwrap_or(path);
            let rel_str = rel.to_string_lossy().replace('\\', "/");
            // The `package` header is the first substantive element of a Kotlin
            // file (after optional file annotations and comments). Stop at the
            // first real line so a later string/identifier containing "package "
            // cannot be mistaken for the declaration; absent a header, the file
            // is in the root package.
            let mut package = String::new();
            for line in BufReader::new(file).lines().map_while(Result::ok) {
                let line = line.trim();
                if line.is_empty()
                    || line.starts_with("//")
                    || line.starts_with("/*")
                    || line.starts_with('*')
                    || line.starts_with('@')
                {
                    continue;
                }
                if let Some(rest) = line.strip_prefix("package ") {
                    package = rest.trim().trim_end_matches(';').trim().to_string();
                }
                break;
            }
            Some((package, rel_str))
        })
        .fold(
            HashMap::<String, Vec<String>>::new,
            |mut acc, (package, rel)| {
                acc.entry(package).or_default().push(rel);
                acc
            },
        )
        .reduce(HashMap::<String, Vec<String>>::new, |mut acc, map| {
            for (package, files) in map {
                acc.entry(package).or_default().extend(files);
            }
            acc
        });
    for files in package_files.values_mut() {
        files.sort();
        files.dedup();
    }
    package_files
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

pub(super) fn build_ruby_constant_files(
    root_path: &Path,
    candidate_files: &[PathBuf],
) -> HashMap<String, Vec<String>> {
    let mut constant_files = candidate_files
        .par_iter()
        .filter_map(|path| {
            let ext = path
                .extension()
                .and_then(|ext| ext.to_str())
                .unwrap_or_default();
            if !matches!(ext, "rb" | "rake" | "gemspec") {
                return None;
            }
            let file = File::open(path).ok()?;
            let rel = path.strip_prefix(root_path).unwrap_or(path);
            let rel_str = rel.to_string_lossy().replace('\\', "/");
            // A Ruby file can declare several top-level constants, and a
            // `class`/`module` line can appear anywhere, so scan every line
            // rather than stopping at the first declaration.
            let mut roots = HashSet::new();
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
            if roots.is_empty() {
                None
            } else {
                Some((rel_str, roots))
            }
        })
        .fold(
            HashMap::<String, Vec<String>>::new,
            |mut acc, (rel, roots)| {
                for root in roots {
                    acc.entry(root).or_default().push(rel.clone());
                }
                acc
            },
        )
        .reduce(HashMap::<String, Vec<String>>::new, |mut all, map| {
            for (root, files) in map {
                all.entry(root).or_default().extend(files);
            }
            all
        });
    for files in constant_files.values_mut() {
        files.sort();
        files.dedup();
    }
    constant_files
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
