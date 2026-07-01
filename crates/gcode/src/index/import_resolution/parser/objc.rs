use crate::models::{ImportRelation, Symbol};

use super::super::context::{
    ExtractedImports, ImportBindings, ImportResolutionContext, LocalCallBinding,
};
use super::super::helpers::extract_quoted_string;

pub(crate) fn parse_objc_import_statement(
    text: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    let trimmed = text.trim();
    if !trimmed.starts_with("#import") && !trimmed.starts_with("#include") {
        return;
    }
    let Some((import_path, is_system)) = objc_import_path(trimmed) else {
        return;
    };

    extracted.imports.push(ImportRelation {
        file_path: rel_path.to_string(),
        module_name: import_path.clone(),
    });

    if is_system {
        return;
    }

    let candidate_files = import_context.objc_import_candidate_files(rel_path, &import_path);
    if candidate_files.is_empty() {
        return;
    }
    extracted
        .bindings
        .objc_import_files
        .extend(candidate_files.iter().cloned());

    for file in candidate_files {
        for function_name in import_context.objc_declared_functions(&file) {
            extracted
                .bindings
                .local_bare
                .entry(function_name.clone())
                .or_insert_with(|| LocalCallBinding::named(Vec::new(), function_name))
                .candidate_files
                .push(file.clone());
        }
        for type_name in import_context.objc_declared_types(&file) {
            extracted
                .bindings
                .local_member
                .entry(type_name)
                .or_default()
                .push(file.clone());
        }
    }
    for files in extracted.bindings.local_member.values_mut() {
        files.sort();
        files.dedup();
    }
    for binding in extracted.bindings.local_bare.values_mut() {
        binding.candidate_files.sort();
        binding.candidate_files.dedup();
    }
    extracted.bindings.objc_import_files.sort();
    extracted.bindings.objc_import_files.dedup();
}

pub(crate) fn resolve_objc_local_callee(
    import_bindings: &ImportBindings,
    symbols: &[Symbol],
    callee_name: &str,
    is_bare_call: bool,
) -> Option<LocalCallBinding> {
    if !is_bare_call || symbols.iter().any(|symbol| symbol.name == callee_name) {
        return None;
    }
    let mut candidate_files = import_bindings.objc_import_files.clone();
    candidate_files.sort();
    candidate_files.dedup();
    (!candidate_files.is_empty())
        .then(|| LocalCallBinding::named(candidate_files, callee_name.to_string()))
}

fn objc_import_path(text: &str) -> Option<(String, bool)> {
    if let Some(path) = extract_quoted_string(text) {
        return Some((path, false));
    }
    let start = text.find('<')?;
    let end = text[start + 1..].find('>')? + start + 1;
    let path = text[start + 1..end].trim();
    (!path.is_empty()).then(|| (path.to_string(), true))
}
