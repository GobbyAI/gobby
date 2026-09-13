use std::collections::{HashMap, HashSet};
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};

use rayon::prelude::*;

use super::super::predicates::java_declared_types;
use crate::index::normalize_storage_path;

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
    let observations = candidate_files
        .par_iter()
        .map(|path| {
            if path.extension().and_then(|ext| ext.to_str()) != Some("java") {
                return (HashSet::new(), HashMap::new());
            }
            let rel = path.strip_prefix(root_path).unwrap_or(path);
            let rel_str = normalize_storage_path(rel);
            std::fs::read(path)
                .ok()
                .map(|source| observe_java_source(&rel_str, &source))
                .unwrap_or_default()
        })
        .collect::<Vec<_>>();
    collect_java_index(observations)
}

fn observe_java_source(
    rel: &str,
    source: &[u8],
) -> (HashSet<String>, HashMap<String, Vec<String>>) {
    let mut local_classes = HashSet::new();
    let mut class_files: HashMap<String, Vec<String>> = HashMap::new();
    if Path::new(rel).extension().and_then(|ext| ext.to_str()) != Some("java") {
        return (local_classes, class_files);
    }
    let mut package = None;
    for line in BufReader::new(source).lines().map_while(Result::ok) {
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
                class_files.entry(fqcn).or_default().push(rel.to_string());
            }
        }
    }
    (local_classes, class_files)
}

fn collect_java_index(
    observations: impl IntoIterator<Item = (HashSet<String>, HashMap<String, Vec<String>>)>,
) -> JavaClassIndex {
    let mut local_classes = HashSet::new();
    let mut class_files: HashMap<String, Vec<String>> = HashMap::new();
    for (classes, files) in observations {
        local_classes.extend(classes);
        for (fqcn, paths) in files {
            class_files.entry(fqcn).or_default().extend(paths);
        }
    }
    for files in class_files.values_mut() {
        files.sort();
        files.dedup();
    }
    JavaClassIndex {
        local_classes,
        class_files,
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
    let observations = candidate_files
        .par_iter()
        .filter_map(|path| {
            let ext = path.extension().and_then(|ext| ext.to_str())?;
            if !matches!(ext, "kt" | "kts") {
                return None;
            }
            let rel = path.strip_prefix(root_path).unwrap_or(path);
            let rel_str = normalize_storage_path(rel);
            observe_kotlin_source(&rel_str, &std::fs::read(path).ok()?)
        })
        .collect::<Vec<_>>();
    collect_package_files(observations)
}

fn observe_kotlin_source(rel: &str, source: &[u8]) -> Option<(String, String)> {
    let ext = Path::new(rel).extension().and_then(|ext| ext.to_str())?;
    if !matches!(ext, "kt" | "kts") {
        return None;
    }
    let mut package = String::new();
    for line in BufReader::new(source).lines().map_while(Result::ok) {
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
    Some((package, rel.to_string()))
}

/// Maps each locally-declared Scala package to the project-relative files that
/// declare it, by reading leading `package` clauses in `.scala` and `.sc`
/// files. Multiple leading package clauses are concatenated (`package a`
/// followed by `package b` means `a.b`). Files with no package clauses belong
/// to the root package.
pub(super) fn build_scala_package_files(
    root_path: &Path,
    candidate_files: &[PathBuf],
) -> HashMap<String, Vec<String>> {
    let observations = candidate_files
        .par_iter()
        .filter_map(|path| {
            let ext = path.extension().and_then(|ext| ext.to_str())?;
            if !matches!(ext, "scala" | "sc") {
                return None;
            }
            let rel = path.strip_prefix(root_path).unwrap_or(path);
            let rel_str = normalize_storage_path(rel);
            observe_scala_source(&rel_str, &std::fs::read(path).ok()?)
        })
        .collect::<Vec<_>>();
    collect_package_files(observations)
}

fn observe_scala_source(rel: &str, source: &[u8]) -> Option<(String, String)> {
    let ext = Path::new(rel).extension().and_then(|ext| ext.to_str())?;
    if !matches!(ext, "scala" | "sc") {
        return None;
    }
    let mut package_segments = Vec::new();
    for line in BufReader::new(source).lines().map_while(Result::ok) {
        let line = line.trim();
        if line.is_empty()
            || line.starts_with("//")
            || line.starts_with("/*")
            || line.starts_with('*')
        {
            continue;
        }
        let Some(rest) = line.strip_prefix("package ") else {
            break;
        };
        let rest = rest.trim();
        if rest.starts_with("object ") {
            break;
        }
        let segment = rest
            .trim_end_matches([';', '{'])
            .split_whitespace()
            .next()
            .unwrap_or_default()
            .trim_end_matches('{')
            .trim();
        if segment.is_empty() {
            break;
        }
        package_segments.push(segment.to_string());
    }
    Some((package_segments.join("."), rel.to_string()))
}

fn collect_package_files(
    observations: impl IntoIterator<Item = (String, String)>,
) -> HashMap<String, Vec<String>> {
    let mut package_files = HashMap::<String, Vec<String>>::new();
    for (package, rel) in observations {
        package_files.entry(package).or_default().push(rel);
    }
    for files in package_files.values_mut() {
        files.sort();
        files.dedup();
    }
    package_files
}
