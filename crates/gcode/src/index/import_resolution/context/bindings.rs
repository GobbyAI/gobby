use std::collections::HashMap;

use crate::models::ImportRelation;

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
    /// Project-relative `.dart` files brought into bare scope by this file's
    /// unaliased local imports (`package:<self>/…` or relative URIs). Dart
    /// imports expose a file's public top-level symbols as a whole with no
    /// per-name binding, so a bare call resolves its name against every one of
    /// these files; the post-write DB pass narrows the set to a real id (or
    /// degrades to unresolved). Aliased local imports feed `local_member`.
    pub(crate) dart_local_import_files: Vec<String>,
    /// Project-relative `.ex`/`.exs` files brought into bare scope by this
    /// file's local `import App.Foo` directives. Elixir `import` exposes a
    /// module's public functions to bare calls with no per-name binding, so a
    /// bare call resolves its name against every one of these files; the
    /// post-write DB pass narrows the set to a real id (or degrades to
    /// unresolved). Local `alias App.Foo` feeds `local_member` instead.
    pub(crate) elixir_local_import_files: Vec<String>,
    /// Project-relative shell files brought into scope by `source`/`.`. Bash
    /// exposes sourced functions to later bare commands without per-name import
    /// syntax, so a bare call resolves its name against these files post-write.
    pub(crate) shell_source_files: Vec<String>,
    /// Project-relative Objective-C headers/sources brought into scope by local
    /// `#import`/`#include` directives. Bare C calls resolve against these files
    /// post-write; Objective-C message sends use `local_member` by receiver
    /// type when the imported file declares an `@interface`/`@protocol`.
    pub(crate) objc_import_files: Vec<String>,
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
pub(in crate::index::import_resolution) const JS_BUILTIN_MODULES: &[&str] = &[
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
