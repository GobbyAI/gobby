use std::collections::{HashMap, HashSet};
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};

use rayon::prelude::*;

pub(super) struct ObjcIndex {
    pub(super) import_files: HashMap<String, Vec<String>>,
    pub(super) file_types: HashMap<String, Vec<String>>,
    pub(super) file_functions: HashMap<String, Vec<String>>,
}

pub(super) fn build_objc_indexes(root_path: &Path, candidate_files: &[PathBuf]) -> ObjcIndex {
    let (mut import_files, mut file_types, mut file_functions) = candidate_files
        .par_iter()
        .filter_map(|path| {
            let ext = path
                .extension()
                .and_then(|ext| ext.to_str())
                .unwrap_or_default();
            if !matches!(ext, "h" | "m" | "mm") {
                return None;
            }
            let rel = path.strip_prefix(root_path).unwrap_or(path);
            let rel_str = rel.to_string_lossy().replace('\\', "/");
            let mut keys = vec![rel_str.clone()];
            if let Some(file_name) = rel.file_name().and_then(|name| name.to_str()) {
                keys.push(file_name.to_string());
            }
            if let Some(without_ext) = rel_str.strip_suffix(&format!(".{ext}")) {
                keys.push(without_ext.to_string());
            }

            let mut types = Vec::new();
            let mut functions = Vec::new();
            if let Ok(file) = File::open(path) {
                for line in BufReader::new(file).lines().map_while(Result::ok) {
                    let line = line.trim_start();
                    if let Some(name) = objc_declared_type_name(line) {
                        types.push(name);
                    }
                    if let Some(name) = objc_declared_function_name(line) {
                        functions.push(name);
                    }
                }
            }
            types.sort();
            types.dedup();
            functions.sort();
            functions.dedup();
            Some((rel_str, keys, types, functions))
        })
        .fold(
            || {
                (
                    HashMap::<String, Vec<String>>::new(),
                    HashMap::<String, Vec<String>>::new(),
                    HashMap::<String, Vec<String>>::new(),
                )
            },
            |mut acc, (rel, keys, types, functions)| {
                for key in keys {
                    acc.0.entry(key).or_default().push(rel.clone());
                }
                if !types.is_empty() {
                    acc.1.insert(rel.clone(), types);
                }
                if !functions.is_empty() {
                    acc.2.insert(rel, functions);
                }
                acc
            },
        )
        .reduce(
            || {
                (
                    HashMap::<String, Vec<String>>::new(),
                    HashMap::<String, Vec<String>>::new(),
                    HashMap::<String, Vec<String>>::new(),
                )
            },
            |mut acc, map| {
                for (key, files) in map.0 {
                    acc.0.entry(key).or_default().extend(files);
                }
                acc.1.extend(map.1);
                acc.2.extend(map.2);
                acc
            },
        );

    for files in import_files.values_mut() {
        files.sort();
        files.dedup();
    }
    for types in file_types.values_mut() {
        types.sort();
        types.dedup();
    }
    for functions in file_functions.values_mut() {
        functions.sort();
        functions.dedup();
    }
    ObjcIndex {
        import_files,
        file_types,
        file_functions,
    }
}

fn objc_declared_type_name(line: &str) -> Option<String> {
    let rest = line
        .strip_prefix("@interface ")
        .or_else(|| line.strip_prefix("@implementation "))
        .or_else(|| line.strip_prefix("@protocol "))?
        .trim_start();
    let name = rest
        .split(|ch: char| ch.is_whitespace() || matches!(ch, ':' | '(' | '<' | ';'))
        .next()
        .unwrap_or_default();
    (!name.is_empty() && is_objc_identifier(name)).then(|| name.to_string())
}

fn objc_declared_function_name(line: &str) -> Option<String> {
    let code = line.split("//").next().unwrap_or(line).trim();
    if code.is_empty()
        || code.starts_with('@')
        || code.starts_with('#')
        || code.contains('=')
        || code.contains("typedef")
    {
        return None;
    }
    let open = code.find('(')?;
    let before = code[..open].trim_end();
    let name = before
        .rsplit(|ch: char| !ch.is_ascii_alphanumeric() && ch != '_')
        .find(|part| !part.is_empty())?;
    if !is_objc_identifier(name)
        || matches!(
            name,
            "if" | "for" | "while" | "switch" | "return" | "sizeof"
        )
    {
        return None;
    }
    Some(name.to_string())
}

pub(super) fn objc_relative_import_file(rel_path: &str, import_path: &str) -> Option<String> {
    if import_path.starts_with(['/', '~', '$'])
        || import_path.contains('$')
        || import_path.contains('`')
        || import_path.contains("$(")
    {
        return None;
    }
    let import = Path::new(import_path);
    if import.components().any(|component| {
        matches!(
            component,
            std::path::Component::Prefix(_) | std::path::Component::RootDir
        )
    }) {
        return None;
    }
    normalize_objc_project_path(&Path::new(rel_path).parent()?.join(import))
}

fn normalize_objc_project_path(path: &Path) -> Option<String> {
    let mut normalized = PathBuf::new();
    for component in path.components() {
        match component {
            std::path::Component::Normal(part) => normalized.push(part),
            std::path::Component::CurDir => {}
            std::path::Component::ParentDir => {
                if !normalized.pop() {
                    return None;
                }
            }
            std::path::Component::Prefix(_) | std::path::Component::RootDir => return None,
        }
    }

    (!normalized.as_os_str().is_empty()).then(|| normalized.to_string_lossy().replace('\\', "/"))
}

fn is_objc_identifier(name: &str) -> bool {
    let mut chars = name.chars();
    let Some(first) = chars.next() else {
        return false;
    };
    (first.is_ascii_alphabetic() || first == '_')
        && chars.all(|ch| ch.is_ascii_alphanumeric() || ch == '_')
}

/// Swift module name(s) a project-relative file belongs to. A file under
/// `Sources/<Module>/` or `Tests/<Module>/` belongs to `<Module>`; any other
/// file is attributed to its immediate parent directory. Swift has whole-module
/// scope, so these names group the files that can call each other without an
/// `import`.
pub(super) fn swift_modules_for_rel(rel: &Path) -> HashSet<String> {
    let mut modules = HashSet::new();
    let components = rel
        .components()
        .filter_map(|component| component.as_os_str().to_str())
        .collect::<Vec<_>>();
    for window in components.windows(2) {
        if matches!(window[0], "Sources" | "Tests") && !window[1].is_empty() {
            modules.insert(window[1].to_string());
        }
    }
    if let Some(parent) = rel
        .parent()
        .and_then(Path::file_name)
        .and_then(|name| name.to_str())
        && !parent.is_empty()
        && parent != "Sources"
        && parent != "Tests"
    {
        modules.insert(parent.to_string());
    }
    modules
}

/// Maps a local Swift module name to the project-relative `.swift` files that
/// belong to it (pure path logic, no file reads). Swift has whole-module scope:
/// files in a module call each other with no `import`, so cross-file call
/// resolution narrows a bare callee name against every file sharing the
/// caller's module. The keys are exactly the locally-defined module names.
pub(in crate::index::import_resolution) fn build_swift_module_files(
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
            if ext != "swift" {
                return None;
            }
            let rel = path.strip_prefix(root_path).unwrap_or(path.as_path());
            let modules = swift_modules_for_rel(rel);
            if modules.is_empty() {
                return None;
            }
            let rel_str = rel.to_string_lossy().replace('\\', "/");
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
