use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

use super::js_local::js_candidate_files;
use super::predicates::ruby_require_root;
use super::rust_local::{rust_candidate_files, rust_import_target, rust_qualified_call_target};

mod apple;
mod bindings;
mod dotnet;
mod elixir;
mod jvm;
mod package_metadata;
mod python;
mod scripting;

pub(super) use bindings::JS_BUILTIN_MODULES;
pub(crate) use bindings::{
    ExternalCallTarget, ExternalImportBinding, ExternalRootBinding, ExtractedImports,
    ImportBindings, LocalCallBinding,
};
pub(super) use package_metadata::{
    build_go_package_files, load_dart_external_packages, load_dart_self_package_name,
    load_go_module_path, load_js_external_packages, load_js_self_package_name,
    load_rust_external_crates, load_rust_self_crate_name,
};
pub(super) use python::{build_python_module_index, python_candidate_files};

pub(super) use apple::build_swift_module_files;
use apple::{build_objc_indexes, objc_relative_import_file, swift_modules_for_rel};
use dotnet::build_csharp_index;
pub(super) use elixir::build_elixir_local_module_files;
#[cfg(test)]
pub(super) use elixir::load_elixir_dependency_names;
use elixir::{build_elixir_local_module_roots, load_elixir_external_roots};
use jvm::{build_java_class_index, build_kotlin_package_files, build_scala_package_files};
pub(super) use scripting::build_php_symbol_files;
use scripting::{build_lua_module_files, build_ruby_constant_files};

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
    /// Maps a locally-declared Scala package (`com.example`) to the
    /// project-relative files declaring it. Scala imports name a package member
    /// (`import com.example.Widget`) or selector group
    /// (`import com.example.{render, Widget}`); either resolves against every
    /// `.scala`/`.sc` file in the package, then the post-write DB pass narrows to
    /// the real symbol.
    pub(super) scala_package_files: HashMap<String, Vec<String>>,
    /// Maps a local Lua module name (`foo.bar`) to project-relative `.lua` files
    /// that may satisfy `require("foo.bar")`. Common source roots (`lua/`,
    /// `src/`) and `init.lua` package modules are normalized into the same key.
    /// Member calls through a require alias resolve against these files, then
    /// the post-write DB pass narrows to the real symbol.
    pub(super) lua_module_files: HashMap<String, Vec<String>>,
    /// Maps Objective-C local import specifiers (`Widget.h`,
    /// `Sources/App/Widget.h`) to project-relative `.h`/`.m`/`.mm` files. The
    /// parser also resolves relative quoted imports directly from the caller's
    /// path; this map covers project-style include paths and basename imports.
    pub(super) objc_import_files: HashMap<String, Vec<String>>,
    /// Maps project-relative Objective-C files to the class/protocol names they
    /// declare. A `#import "Widget.h"` can then bind receiver type `Widget` to
    /// that header's candidate file without guessing from the import basename.
    pub(super) objc_file_types: HashMap<String, Vec<String>>,
    /// Maps project-relative Objective-C files to C function names they declare
    /// or define. A local `#import` seeds exact bare-call bindings from this
    /// map, so unrelated imported headers do not become candidates.
    pub(super) objc_file_functions: HashMap<String, Vec<String>>,
    pub(super) php_local_symbols: HashSet<String>,
    /// Maps a locally-declared PHP symbol name to the project-relative files that
    /// declare it. Both the bare name (`Widget`, `helper`) and the
    /// namespace-qualified name (`App\Widget`) are keyed, lowercased because PHP
    /// class/function names are case-insensitive. A `use`-imported local class
    /// resolves `Widget::m()` / `new Widget()` against these files, and a
    /// fully-qualified `\Ns\Class::method()` resolves its class path here. The
    /// post-write DB pass narrows the candidates to the real symbol.
    pub(super) php_symbol_files: HashMap<String, Vec<String>>,
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
    /// Maps a local Swift module name to the project-relative `.swift` files that
    /// belong to it. Swift has whole-module scope: files in a module call each
    /// other with no `import`, so an unqualified call resolves against every file
    /// sharing the caller's module. The post-write DB pass narrows the callee
    /// name to the real symbol. The keys are exactly `swift_local_modules`.
    pub(super) swift_module_files: HashMap<String, Vec<String>>,
    pub(super) dart_external_packages: HashSet<String>,
    pub(super) dart_self_package_name: Option<String>,
    pub(super) elixir_external_roots: HashMap<String, String>,
    pub(super) elixir_external_root_overrides: HashMap<String, String>,
    pub(super) elixir_local_module_roots: HashSet<String>,
    /// Maps a locally-declared Elixir module's fully-qualified name (`App.Foo`)
    /// to the project-relative `.ex`/`.exs` files that declare it (via
    /// `defmodule`). A fully-qualified `App.Foo.func()` call resolves `App.Foo`
    /// here, and a local `alias App.Foo` / `import App.Foo` seeds the alias and
    /// bare channels from these files. Modules need not follow the
    /// path-from-name convention, so the map is built by scanning `defmodule`
    /// headers. The post-write DB pass narrows the candidates to the real
    /// symbol.
    pub(super) elixir_module_files: HashMap<String, Vec<String>>,
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
        let target = rust_import_target(
            rel_path,
            self.rust_self_crate_name.as_deref(),
            &self.rust_external_crates,
            path,
        )?;
        Some(LocalCallBinding::named(
            rust_candidate_files(&target.source_root, &target.module),
            target.name,
        ))
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
            &self.rust_external_crates,
            qualifier_path,
            name,
        )?;
        Some(LocalCallBinding::named(
            rust_candidate_files(&target.source_root, &target.module),
            target.name,
        ))
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

    /// Project-relative files declaring the local Scala `package`. Empty when no
    /// indexed local file declares that package (external import or unknown
    /// package).
    pub(super) fn scala_package_files(&self, package: &str) -> Vec<String> {
        self.scala_package_files
            .get(package)
            .cloned()
            .unwrap_or_default()
    }

    pub(super) fn lua_module_files(&self, module: &str) -> Vec<String> {
        self.lua_module_files
            .get(module)
            .cloned()
            .unwrap_or_default()
    }

    pub(super) fn objc_import_candidate_files(
        &self,
        rel_path: &str,
        import_path: &str,
    ) -> Vec<String> {
        let mut files = Vec::new();
        if let Some(relative) = objc_relative_import_file(rel_path, import_path) {
            files.push(relative);
        }
        if let Some(mapped) = self.objc_import_files.get(import_path) {
            files.extend(mapped.iter().cloned());
        }
        if let Some(name) = Path::new(import_path)
            .file_name()
            .and_then(|name| name.to_str())
            && let Some(mapped) = self.objc_import_files.get(name)
        {
            files.extend(mapped.iter().cloned());
        }
        files.sort();
        files.dedup();
        files
    }

    pub(super) fn objc_declared_types(&self, rel_path: &str) -> Vec<String> {
        self.objc_file_types
            .get(rel_path)
            .cloned()
            .unwrap_or_default()
    }

    pub(super) fn objc_declared_functions(&self, rel_path: &str) -> Vec<String> {
        self.objc_file_functions
            .get(rel_path)
            .cloned()
            .unwrap_or_default()
    }

    /// Project-relative files declaring the local PHP class/function named
    /// `name`. Accepts a bare name or a namespace-qualified name, with or without
    /// a leading `\`; matching is case-insensitive. Empty when no indexed local
    /// file declares it (an external/vendor symbol, or an unknown name).
    pub(super) fn php_candidate_files(&self, name: &str) -> Vec<String> {
        self.php_symbol_files
            .get(&name.trim_start_matches('\\').to_ascii_lowercase())
            .cloned()
            .unwrap_or_default()
    }

    /// Project-relative `.swift` files that share a Swift module with `rel_path`
    /// (including `rel_path` itself). Swift's whole-module scope means an
    /// unqualified call may target any file in the caller's module; the
    /// post-write DB pass narrows the callee name to a single symbol. Empty when
    /// the file belongs to no discovered module.
    pub(super) fn swift_module_candidate_files(&self, rel_path: &str) -> Vec<String> {
        let mut files = swift_modules_for_rel(Path::new(rel_path))
            .iter()
            .filter_map(|module| self.swift_module_files.get(module))
            .flatten()
            .cloned()
            .collect::<Vec<_>>();
        files.sort();
        files.dedup();
        files
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

    /// Project-relative files declaring the local Elixir module named `module`
    /// (a fully-qualified `App.Foo`). Empty when no indexed local file declares
    /// it (an external/dependency module, or an unknown name).
    pub(super) fn elixir_module_files(&self, module: &str) -> Vec<String> {
        self.elixir_module_files
            .get(module)
            .cloned()
            .unwrap_or_default()
    }
}

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
    let php_symbol_files = build_php_symbol_files(root_path, candidate_files);
    let swift_module_files = build_swift_module_files(root_path, candidate_files);
    let objc_index = build_objc_indexes(root_path, candidate_files);
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
        scala_package_files: build_scala_package_files(root_path, candidate_files),
        lua_module_files: build_lua_module_files(root_path, candidate_files),
        objc_import_files: objc_index.import_files,
        objc_file_types: objc_index.file_types,
        objc_file_functions: objc_index.file_functions,
        php_local_symbols: php_symbol_files.keys().cloned().collect(),
        php_symbol_files,
        ruby_local_constant_roots: ruby_constant_files.keys().cloned().collect(),
        ruby_constant_files,
        ruby_require_root_overrides,
        swift_local_modules: swift_module_files.keys().cloned().collect(),
        swift_module_files,
        dart_external_packages: load_dart_external_packages(root_path),
        dart_self_package_name: load_dart_self_package_name(root_path),
        elixir_external_roots: load_elixir_external_roots(root_path),
        elixir_external_root_overrides,
        elixir_local_module_roots: build_elixir_local_module_roots(candidate_files),
        elixir_module_files: build_elixir_local_module_files(root_path, candidate_files),
    }
}
