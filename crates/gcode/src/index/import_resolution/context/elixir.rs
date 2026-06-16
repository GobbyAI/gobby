use std::collections::{HashMap, HashSet};
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use rayon::prelude::*;
use regex::Regex;

use super::super::helpers::{is_elixir_alias, is_elixir_alias_path};
use super::super::predicates::elixir_dependency_roots;

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

/// Maps each locally-declared Elixir module's fully-qualified name to the
/// project-relative `.ex`/`.exs` files that declare it. Built by scanning
/// `defmodule` headers because Elixir modules do not have to follow the
/// path-from-name convention and a single file can declare several modules.
pub(in crate::index::import_resolution) fn build_elixir_local_module_files(
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
            if !matches!(ext, "ex" | "exs") {
                return None;
            }
            let file = File::open(path).ok()?;
            let rel = path.strip_prefix(root_path).unwrap_or(path.as_path());
            let rel_str = rel.to_string_lossy().replace('\\', "/");
            let mut modules: Vec<String> = Vec::new();
            for line in BufReader::new(file).lines().map_while(Result::ok) {
                let line = line.trim_start();
                let Some(rest) = line.strip_prefix("defmodule ") else {
                    continue;
                };
                let module = rest
                    .split(|ch: char| ch.is_whitespace() || matches!(ch, ',' | '(' | '['))
                    .next()
                    .unwrap_or_default();
                if !module.is_empty() && is_elixir_alias_path(module) {
                    modules.push(module.to_string());
                }
            }
            if modules.is_empty() {
                return None;
            }
            Some((rel_str, modules))
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
        .reduce(HashMap::<String, Vec<String>>::new, |mut all, map| {
            for (module, files) in map {
                all.entry(module).or_default().extend(files);
            }
            all
        });
    for files in module_files.values_mut() {
        files.sort();
        files.dedup();
    }
    module_files
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

pub(in crate::index::import_resolution) fn load_elixir_dependency_names(
    root_path: &Path,
) -> HashSet<String> {
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
