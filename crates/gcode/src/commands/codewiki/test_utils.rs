use crate::models::Symbol;

pub(crate) fn test_symbol(
    file_path: &str,
    name: &str,
    kind: &str,
    line_start: usize,
    signature: &str,
) -> Symbol {
    test_symbol_with_qualified(file_path, name, name, kind, line_start, signature)
}

pub(crate) fn test_component_id(file_path: &str, name: &str, kind: &str) -> String {
    Symbol::make_id("project-1", file_path, name, kind, 0)
}

pub(crate) fn test_symbol_with_qualified(
    file_path: &str,
    name: &str,
    qualified_name: &str,
    kind: &str,
    line_start: usize,
    signature: &str,
) -> Symbol {
    Symbol {
        id: test_component_id(file_path, name, kind),
        project_id: "project-1".to_string(),
        file_path: file_path.to_string(),
        name: name.to_string(),
        qualified_name: qualified_name.to_string(),
        kind: kind.to_string(),
        language: "rust".to_string(),
        byte_start: 0,
        byte_end: 0,
        line_start,
        line_end: line_start,
        signature: Some(signature.to_string()),
        docstring: None,
        parent_symbol_id: None,
        content_hash: String::new(),
        summary: None,
        created_at: String::new(),
        updated_at: String::new(),
    }
}

pub(crate) fn test_symbol_range(
    file_path: &str,
    name: &str,
    kind: &str,
    line_start: usize,
    line_end: usize,
    signature: &str,
) -> Symbol {
    Symbol {
        id: test_component_id(file_path, name, kind),
        project_id: "project-1".to_string(),
        file_path: file_path.to_string(),
        name: name.to_string(),
        qualified_name: name.to_string(),
        kind: kind.to_string(),
        language: "rust".to_string(),
        byte_start: 0,
        byte_end: 0,
        line_start,
        line_end,
        signature: Some(signature.to_string()),
        docstring: None,
        parent_symbol_id: None,
        content_hash: String::new(),
        summary: None,
        created_at: String::new(),
        updated_at: String::new(),
    }
}
