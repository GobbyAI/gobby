use std::collections::{HashMap, HashSet};
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};

use rayon::prelude::*;

use super::super::predicates::csharp_declared_types;
use crate::index::normalize_storage_path;

pub(super) struct CsharpIndex {
    /// Namespace roots and simple type names declared by local files. Used to
    /// classify a `using` target as local (`is_external_csharp_path`).
    pub(super) local_roots: HashSet<String>,
    /// Fully-qualified type name (`Ns.Type`) -> declaring project-relative
    /// files. Used to map a local member call to its target file(s).
    pub(super) type_files: HashMap<String, Vec<String>>,
}

pub(super) fn build_csharp_index(root_path: &Path, candidate_files: &[PathBuf]) -> CsharpIndex {
    let observations = candidate_files
        .par_iter()
        .map(|path| {
            if path.extension().and_then(|ext| ext.to_str()) != Some("cs") {
                return (HashSet::new(), HashMap::new());
            }
            let rel = path.strip_prefix(root_path).unwrap_or(path);
            let rel_str = normalize_storage_path(rel);
            std::fs::read(path)
                .ok()
                .map(|source| observe_csharp_source(&rel_str, &source))
                .unwrap_or_default()
        })
        .collect::<Vec<_>>();
    collect_csharp_index(observations)
}

pub(super) fn build_csharp_index_from_sources(
    sources: &crate::index::captured_sources::CapturedSources<'_>,
) -> CsharpIndex {
    collect_csharp_index(
        sources
            .iter()
            .map(|(rel, source)| observe_csharp_source(rel, source)),
    )
}

fn observe_csharp_source(
    rel: &str,
    source: &[u8],
) -> (HashSet<String>, HashMap<String, Vec<String>>) {
    let mut local_roots = HashSet::new();
    let mut type_files: HashMap<String, Vec<String>> = HashMap::new();
    if Path::new(rel).extension().and_then(|ext| ext.to_str()) != Some("cs") {
        return (local_roots, type_files);
    }
    let mut current_namespace: Option<String> = None;
    for line in BufReader::new(source).lines().map_while(Result::ok) {
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
                type_files
                    .entry(format!("{namespace}.{type_name}"))
                    .or_default()
                    .push(rel.to_string());
            }
        }
    }
    (local_roots, type_files)
}

fn collect_csharp_index(
    observations: impl IntoIterator<Item = (HashSet<String>, HashMap<String, Vec<String>>)>,
) -> CsharpIndex {
    let mut local_roots = HashSet::new();
    let mut type_files: HashMap<String, Vec<String>> = HashMap::new();
    for (roots, files) in observations {
        local_roots.extend(roots);
        for (fqcn, paths) in files {
            type_files.entry(fqcn).or_default().extend(paths);
        }
    }
    for files in type_files.values_mut() {
        files.sort();
        files.dedup();
    }
    CsharpIndex {
        local_roots,
        type_files,
    }
}
