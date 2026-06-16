use std::collections::{HashMap, HashSet};
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};

use rayon::prelude::*;

use super::super::predicates::java_declared_types;

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
    let (local_classes, mut class_files) = candidate_files
        .par_iter()
        .map(|path| {
            let mut local_classes = HashSet::new();
            let mut class_files: HashMap<String, Vec<String>> = HashMap::new();
            let ext = path
                .extension()
                .and_then(|ext| ext.to_str())
                .unwrap_or_default();
            if ext != "java" {
                return (local_classes, class_files);
            }
            let Ok(file) = File::open(path) else {
                return (local_classes, class_files);
            };
            let rel = path.strip_prefix(root_path).unwrap_or(path);
            let rel_str = rel.to_string_lossy().replace('\\', "/");
            let mut package = None;
            for line in BufReader::new(file).lines().map_while(Result::ok) {
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
                        class_files.entry(fqcn).or_default().push(rel_str.clone());
                    }
                }
            }
            (local_classes, class_files)
        })
        .reduce(
            || (HashSet::new(), HashMap::new()),
            |mut acc, (classes, files)| {
                acc.0.extend(classes);
                for (fqcn, paths) in files {
                    acc.1.entry(fqcn).or_default().extend(paths);
                }
                acc
            },
        );
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
    let mut package_files = candidate_files
        .par_iter()
        .filter_map(|path| {
            let ext = path
                .extension()
                .and_then(|ext| ext.to_str())
                .unwrap_or_default();
            if ext != "kt" && ext != "kts" {
                return None;
            }
            let file = File::open(path).ok()?;
            let rel = path.strip_prefix(root_path).unwrap_or(path);
            let rel_str = rel.to_string_lossy().replace('\\', "/");
            // The `package` header is the first substantive element of a Kotlin
            // file (after optional file annotations and comments). Stop at the
            // first real line so a later string/identifier containing "package "
            // cannot be mistaken for the declaration; absent a header, the file
            // is in the root package.
            let mut package = String::new();
            for line in BufReader::new(file).lines().map_while(Result::ok) {
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
            Some((package, rel_str))
        })
        .fold(
            HashMap::<String, Vec<String>>::new,
            |mut acc, (package, rel)| {
                acc.entry(package).or_default().push(rel);
                acc
            },
        )
        .reduce(HashMap::<String, Vec<String>>::new, |mut acc, map| {
            for (package, files) in map {
                acc.entry(package).or_default().extend(files);
            }
            acc
        });
    for files in package_files.values_mut() {
        files.sort();
        files.dedup();
    }
    package_files
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
    let mut package_files = candidate_files
        .par_iter()
        .filter_map(|path| {
            let ext = path
                .extension()
                .and_then(|ext| ext.to_str())
                .unwrap_or_default();
            if ext != "scala" && ext != "sc" {
                return None;
            }
            let file = File::open(path).ok()?;
            let rel = path.strip_prefix(root_path).unwrap_or(path);
            let rel_str = rel.to_string_lossy().replace('\\', "/");
            let mut package_segments = Vec::new();
            for line in BufReader::new(file).lines().map_while(Result::ok) {
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
            Some((package_segments.join("."), rel_str))
        })
        .fold(
            HashMap::<String, Vec<String>>::new,
            |mut acc, (package, rel)| {
                acc.entry(package).or_default().push(rel);
                acc
            },
        )
        .reduce(HashMap::<String, Vec<String>>::new, |mut acc, map| {
            for (package, files) in map {
                acc.entry(package).or_default().extend(files);
            }
            acc
        });
    for files in package_files.values_mut() {
        files.sort();
        files.dedup();
    }
    package_files
}
