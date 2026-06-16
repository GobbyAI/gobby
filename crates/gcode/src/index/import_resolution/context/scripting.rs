use std::collections::{HashMap, HashSet};
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};

use rayon::prelude::*;

use super::super::helpers::is_ruby_constant_name;
use super::super::predicates::php_declared_symbols;

pub(super) fn build_lua_module_files(
    root_path: &Path,
    candidate_files: &[PathBuf],
) -> HashMap<String, Vec<String>> {
    let mut module_files = candidate_files
        .par_iter()
        .filter_map(|path| {
            let ext = path
                .extension()
                .and_then(|ext| ext.to_str())
                .unwrap_or_default();
            if ext != "lua" {
                return None;
            }
            let rel = path.strip_prefix(root_path).unwrap_or(path);
            let rel_str = rel.to_string_lossy().replace('\\', "/");
            let without_ext = rel_str.strip_suffix(".lua")?;
            let modules = lua_module_names_for_path(without_ext);
            if modules.is_empty() {
                None
            } else {
                Some((rel_str, modules))
            }
        })
        .fold(
            HashMap::<String, Vec<String>>::new,
            |mut acc, (rel, modules)| {
                for module in modules {
                    acc.entry(module).or_default().push(rel.clone());
                }
                acc
            },
        )
        .reduce(HashMap::<String, Vec<String>>::new, |mut acc, map| {
            for (module, files) in map {
                acc.entry(module).or_default().extend(files);
            }
            acc
        });
    for files in module_files.values_mut() {
        files.sort();
        files.dedup();
    }
    module_files
}

fn lua_module_names_for_path(without_ext: &str) -> HashSet<String> {
    let mut modules = HashSet::new();
    add_lua_module_names(&mut modules, without_ext);
    for prefix in ["lua/", "src/"] {
        if let Some(stripped) = without_ext.strip_prefix(prefix) {
            add_lua_module_names(&mut modules, stripped);
        }
    }
    modules
}

fn add_lua_module_names(modules: &mut HashSet<String>, without_ext: &str) {
    let module = without_ext.trim_matches('/');
    if module.is_empty() {
        return;
    }
    if let Some(package) = module.strip_suffix("/init") {
        insert_lua_module_name(modules, package);
    }
    insert_lua_module_name(modules, module);
}

fn insert_lua_module_name(modules: &mut HashSet<String>, module: &str) {
    let module = module.replace('/', ".");
    if !module.is_empty() {
        modules.insert(module);
    }
}

pub(in crate::index::import_resolution) fn build_php_symbol_files(
    root_path: &Path,
    candidate_files: &[PathBuf],
) -> HashMap<String, Vec<String>> {
    let mut symbol_files = candidate_files
        .par_iter()
        .filter_map(|path| {
            let ext = path
                .extension()
                .and_then(|ext| ext.to_str())
                .unwrap_or_default();
            if ext != "php" {
                return None;
            }
            let file = File::open(path).ok()?;
            let rel = path.strip_prefix(root_path).unwrap_or(path);
            let rel_str = rel.to_string_lossy().replace('\\', "/");
            // A symbol is keyed under both its bare name and (when the file
            // declares a namespace) its `namespace\name` qualified form, both
            // lowercased because PHP class/function names are case-insensitive.
            let mut namespace = None;
            let mut names = HashSet::new();
            for line in BufReader::new(file).lines().map_while(Result::ok) {
                let line = line.trim();
                if namespace.is_none() {
                    namespace = line
                        .strip_prefix("namespace ")
                        .map(|rest| rest.trim().trim_end_matches([';', '{']).to_string());
                }
                for name in php_declared_symbols(line) {
                    names.insert(name.to_ascii_lowercase());
                    if let Some(namespace) = namespace.as_deref()
                        && !namespace.is_empty()
                    {
                        names.insert(format!("{namespace}\\{name}").to_ascii_lowercase());
                    }
                }
            }
            if names.is_empty() {
                None
            } else {
                Some((rel_str, names))
            }
        })
        .fold(
            HashMap::<String, Vec<String>>::new,
            |mut acc, (rel, names)| {
                for name in names {
                    acc.entry(name).or_default().push(rel.clone());
                }
                acc
            },
        )
        .reduce(HashMap::<String, Vec<String>>::new, |mut all, map| {
            for (name, files) in map {
                all.entry(name).or_default().extend(files);
            }
            all
        });
    for files in symbol_files.values_mut() {
        files.sort();
        files.dedup();
    }
    symbol_files
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
