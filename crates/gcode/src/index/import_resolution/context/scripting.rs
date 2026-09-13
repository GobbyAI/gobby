use std::collections::{HashMap, HashSet};
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};

use rayon::prelude::*;

use super::super::helpers::is_ruby_constant_name;
use super::super::predicates::php_declared_symbols;
use crate::index::normalize_storage_path;

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
            let rel_str = normalize_storage_path(rel);
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
    let observations = candidate_files
        .par_iter()
        .filter_map(|path| {
            if path.extension().and_then(|ext| ext.to_str()) != Some("php") {
                return None;
            }
            let rel = path.strip_prefix(root_path).unwrap_or(path);
            let rel_str = normalize_storage_path(rel);
            observe_php_source(&rel_str, &std::fs::read(path).ok()?)
        })
        .collect::<Vec<_>>();
    collect_named_files(observations)
}

fn observe_php_source(rel: &str, source: &[u8]) -> Option<(String, HashSet<String>)> {
    if Path::new(rel).extension().and_then(|ext| ext.to_str()) != Some("php") {
        return None;
    }
    let mut namespace = None;
    let mut names = HashSet::new();
    for line in BufReader::new(source).lines().map_while(Result::ok) {
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
    (!names.is_empty()).then(|| (rel.to_string(), names))
}

pub(super) fn build_ruby_constant_files(
    root_path: &Path,
    candidate_files: &[PathBuf],
) -> HashMap<String, Vec<String>> {
    let observations = candidate_files
        .par_iter()
        .filter_map(|path| {
            let ext = path.extension().and_then(|ext| ext.to_str())?;
            if !matches!(ext, "rb" | "rake") {
                return None;
            }
            let rel = path.strip_prefix(root_path).unwrap_or(path);
            let rel_str = normalize_storage_path(rel);
            observe_ruby_source(&rel_str, &std::fs::read(path).ok()?)
        })
        .collect::<Vec<_>>();
    collect_named_files(observations)
}

fn observe_ruby_source(rel: &str, source: &[u8]) -> Option<(String, HashSet<String>)> {
    let ext = Path::new(rel).extension().and_then(|ext| ext.to_str())?;
    if !matches!(ext, "rb" | "rake" | "gemspec") {
        return None;
    }
    let mut roots = HashSet::new();
    for line in BufReader::new(source).lines().map_while(Result::ok) {
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
    (!roots.is_empty()).then(|| (rel.to_string(), roots))
}

fn collect_named_files(
    observations: impl IntoIterator<Item = (String, HashSet<String>)>,
) -> HashMap<String, Vec<String>> {
    let mut files_by_name = HashMap::<String, Vec<String>>::new();
    for (rel, names) in observations {
        for name in names {
            files_by_name.entry(name).or_default().push(rel.clone());
        }
    }
    sort_file_map(&mut files_by_name);
    files_by_name
}

fn sort_file_map(map: &mut HashMap<String, Vec<String>>) {
    for files in map.values_mut() {
        files.sort();
        files.dedup();
    }
}
