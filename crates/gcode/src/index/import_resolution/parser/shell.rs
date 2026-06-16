use std::path::{Component, Path, PathBuf};

use crate::models::{ImportRelation, Symbol};

use super::super::context::{ExtractedImports, ImportBindings, LocalCallBinding};
use super::super::helpers::{collapse_whitespace, extract_quoted_string};

pub(crate) fn parse_shell_import_statement(
    text: &str,
    rel_path: &str,
    extracted: &mut ExtractedImports,
) {
    let normalized = collapse_whitespace(text);
    let Some((command, rest)) = normalized.split_once(' ') else {
        return;
    };
    if !matches!(command, "source" | ".") {
        return;
    }

    let source_path = extract_quoted_string(&normalized).unwrap_or_else(|| {
        rest.split_whitespace()
            .next()
            .unwrap_or_default()
            .to_string()
    });
    if source_path.is_empty() {
        return;
    }

    extracted.imports.push(ImportRelation {
        file_path: rel_path.to_string(),
        module_name: source_path.clone(),
    });

    let Some(target_file) = shell_source_target(rel_path, &source_path) else {
        return;
    };
    extracted.bindings.shell_source_files.push(target_file);
}

pub(crate) fn resolve_shell_local_callee(
    import_bindings: &ImportBindings,
    symbols: &[Symbol],
    callee_name: &str,
    is_bare_call: bool,
) -> Option<LocalCallBinding> {
    if !is_bare_call || symbols.iter().any(|symbol| symbol.name == callee_name) {
        return None;
    }

    let mut candidate_files = import_bindings.shell_source_files.clone();
    candidate_files.sort();
    candidate_files.dedup();
    (!candidate_files.is_empty())
        .then(|| LocalCallBinding::named(candidate_files, callee_name.to_string()))
}

fn shell_source_target(rel_path: &str, source_path: &str) -> Option<String> {
    if source_path.starts_with(['/', '~', '$'])
        || source_path.contains('$')
        || source_path.contains('`')
        || source_path.contains("$(")
    {
        return None;
    }
    let source = Path::new(source_path);
    if source
        .components()
        .any(|component| matches!(component, Component::Prefix(_) | Component::RootDir))
    {
        return None;
    }
    if source.components().next() == Some(Component::ParentDir) {
        return normalize_project_path(&Path::new(rel_path).parent()?.join(source));
    }
    normalize_project_path(&Path::new(rel_path).parent()?.join(source))
}

fn normalize_project_path(path: &Path) -> Option<String> {
    let mut normalized = PathBuf::new();
    for component in path.components() {
        match component {
            Component::Normal(part) => normalized.push(part),
            Component::CurDir => {}
            Component::ParentDir => {
                if !normalized.pop() {
                    return None;
                }
            }
            Component::Prefix(_) | Component::RootDir => return None,
        }
    }

    (!normalized.as_os_str().is_empty()).then(|| normalized.to_string_lossy().replace('\\', "/"))
}
