use std::fs;
use std::path::Path;

pub(super) use std::collections::{HashMap, HashSet};
pub(super) use tempfile::TempDir;

pub(super) use super::super::context::{
    build_elixir_local_module_files, build_php_symbol_files, build_swift_module_files,
    load_dart_external_packages, load_elixir_dependency_names, load_js_external_packages,
    load_rust_external_crates, load_rust_self_crate_name,
};
pub(super) use super::super::helpers::{
    dart_local_import_target, extract_quoted_string, go_default_package_alias, split_top_level,
};
pub(super) use super::super::predicates::{
    bundled_elixir_dependency_roots, bundled_ruby_require_roots, csharp_declared_types,
    elixir_dependency_roots, is_external_java_class, is_external_js_module, java_declared_types,
    php_declared_symbols, ruby_require_root,
};
pub(super) use super::super::*;

pub(super) fn manifest_dir(root: &Path, name: &str, body: &str) {
    let dir = root.join(name);
    fs::create_dir_all(&dir).expect("manifest dir");
    fs::write(dir.join("Cargo.toml"), body).expect("cargo toml");
}
