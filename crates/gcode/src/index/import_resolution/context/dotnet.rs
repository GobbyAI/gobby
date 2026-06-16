use std::collections::{HashMap, HashSet};
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};

use rayon::prelude::*;

use super::super::predicates::csharp_declared_types;

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
