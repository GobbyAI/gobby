use super::*;

pub(crate) fn inline_code(value: &str) -> String {
    let value = value.replace('\n', " ");
    if value.is_empty() {
        return "``".to_string();
    }
    let delimiter = "`".repeat(max_backtick_run(&value).saturating_add(1));
    if value.starts_with('`') || value.ends_with('`') {
        format!("{delimiter} {value} {delimiter}")
    } else {
        format!("{delimiter}{value}{delimiter}")
    }
}

pub(crate) fn max_backtick_run(value: &str) -> usize {
    let mut max_run = 0usize;
    let mut current_run = 0usize;
    for ch in value.chars() {
        if ch == '`' {
            current_run += 1;
            max_run = max_run.max(current_run);
        } else {
            current_run = 0;
        }
    }
    max_run
}

pub(crate) fn plural(count: usize) -> &'static str {
    if count == 1 { "" } else { "s" }
}

pub(crate) fn component_id(symbol: &Symbol) -> String {
    symbol.id.clone()
}

pub(crate) fn component_label(symbol: &Symbol) -> String {
    let name = if symbol.qualified_name.is_empty() {
        &symbol.name
    } else {
        &symbol.qualified_name
    };
    format!("{name} [{}]", symbol.kind)
}

pub(crate) fn is_core_file(file: &str) -> bool {
    let lower = file.to_ascii_lowercase();
    if lower.contains(".generated.")
        || lower.ends_with(".generated.rs")
        || lower.ends_with(".gen.rs")
        || lower.contains(".test.")
        || lower.contains(".spec.")
        || lower.ends_with("_spec.rs")
        || lower.ends_with(".spec.rs")
        || lower.ends_with("_test.rs")
        || lower.ends_with("_tests.rs")
    {
        return false;
    }
    if Path::new(file)
        .file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.to_ascii_lowercase().starts_with("test_"))
    {
        return false;
    }
    !Path::new(file).components().any(|component| {
        let part = component.as_os_str().to_string_lossy().to_ascii_lowercase();
        matches!(
            part.as_str(),
            "test"
                | "tests"
                | "__tests__"
                | "__mocks__"
                | "mocks"
                | "spec"
                | "specs"
                | "fixture"
                | "fixtures"
                | "vendor"
                | "vendored"
                | "third_party"
                | "generated"
                | "gen"
                | "dist"
                | "build"
                | "target"
                | "node_modules"
        )
    })
}

pub(crate) fn in_scope(file: &str, scopes: &[String]) -> bool {
    // No scope filter, or an explicitly empty scope, means include every file.
    scopes.is_empty()
        || scopes.iter().any(|scope| scope.is_empty())
        || scopes.iter().any(|scope| {
            file == scope || file.starts_with(&format!("{}/", scope.trim_end_matches('/')))
        })
}

pub(crate) fn module_for_file(file: &str) -> String {
    Path::new(file)
        .parent()
        .map(|path| path.to_string_lossy().replace('\\', "/"))
        .filter(|path| path != ".")
        .unwrap_or_default()
}

pub(crate) fn module_ancestors(module: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut current = module;
    while !current.is_empty() {
        out.push(current.to_string());
        current = parent_module(current).unwrap_or("");
    }
    out
}

pub(crate) fn parent_module(module: &str) -> Option<&str> {
    module.rsplit_once('/').map(|(parent, _)| parent)
}

pub(crate) fn module_is_ancestor(module: &str, child: &str) -> bool {
    !module.is_empty() && child.starts_with(&format!("{module}/"))
}

pub(crate) fn direct_child_modules<'a>(
    module: &str,
    candidates: impl Iterator<Item = &'a String>,
) -> Vec<String> {
    candidates
        .filter(|candidate| parent_module(candidate).is_some_and(|parent| parent == module))
        .cloned()
        .collect()
}

pub(crate) fn module_depth(module: &str) -> usize {
    module.split('/').filter(|part| !part.is_empty()).count()
}

pub(crate) fn file_doc_path(file: &str) -> String {
    format!("files/{file}.md")
}

pub(crate) fn module_doc_path(module: &str) -> String {
    format!("modules/{module}.md")
}

pub(crate) fn file_wikilink(file: &str) -> String {
    format!("[[files/{file}|{file}]]")
}

pub(crate) fn module_wikilink(module: &str) -> String {
    format!("[[modules/{module}|{module}]]")
}
