use std::collections::{HashMap, HashSet};
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use rayon::prelude::*;
use regex::Regex;

use super::super::helpers::{is_elixir_alias, is_elixir_alias_path};
use super::super::predicates::elixir_dependency_roots;
use crate::index::normalize_storage_path;

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
            let Ok(source) = std::fs::read(path) else {
                return roots;
            };
            for module in elixir_module_names(&source) {
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
            let source = std::fs::read(path).ok()?;
            let rel = path.strip_prefix(root_path).unwrap_or(path.as_path());
            let rel_str = normalize_storage_path(rel);
            let modules = elixir_module_names(&source);
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

fn elixir_module_names(source: &[u8]) -> Vec<String> {
    BufReader::new(source)
        .lines()
        .map_while(Result::ok)
        .filter_map(|line| {
            let rest = line.trim_start().strip_prefix("defmodule ")?;
            let module = rest
                .split(|ch: char| ch.is_whitespace() || matches!(ch, ',' | '(' | '['))
                .next()
                .unwrap_or_default();
            (!module.is_empty() && is_elixir_alias_path(module)).then(|| module.to_string())
        })
        .collect()
}

pub(super) fn load_elixir_external_roots(root_path: &Path) -> HashMap<String, String> {
    let deps = load_elixir_dependency_names(root_path);
    elixir_external_roots(deps)
}

fn elixir_external_roots(deps: HashSet<String>) -> HashMap<String, String> {
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
    let mix_exs = std::fs::read(root_path.join("mix.exs")).ok();
    let mix_lock = std::fs::read(root_path.join("mix.lock")).ok();
    elixir_dependency_names(mix_exs.as_deref(), mix_lock.as_deref())
}

fn elixir_dependency_names(mix_exs: Option<&[u8]>, mix_lock: Option<&[u8]>) -> HashSet<String> {
    let mut deps = HashSet::new();
    if let Some(contents) = mix_exs.and_then(|contents| std::str::from_utf8(contents).ok()) {
        // This is a whole-file manifest heuristic, not an Elixir parser. It catches
        // normal deps entries even when tuple formatting spans lines.
        for captures in elixir_mix_dependency_regex().captures_iter(contents) {
            if let Some(dep) = captures.get(1) {
                deps.insert(dep.as_str().to_string());
            }
        }
    }
    if let Some(contents) = mix_lock.and_then(|contents| std::str::from_utf8(contents).ok()) {
        // Lockfiles are Elixir maps; quoted dependency keys are enough here. Values
        // may contain package names and repository names that should not be indexed.
        for captures in elixir_lock_dependency_regex().captures_iter(contents) {
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
