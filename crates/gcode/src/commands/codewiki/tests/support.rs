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

pub(crate) fn test_curated_navigation_json() -> String {
    r#"{
      "concept_modules": [
        {
          "title": "Source Tour",
          "summary": "Reader-oriented entry points into the source reference.",
          "modules": ["src"],
          "files": ["src/lib.rs"]
        }
      ],
      "sections": [
        {
          "title": "Start Here",
          "summary": "Begin with source structure, then follow grounded references.",
          "concepts": ["Source Tour"]
        }
      ],
      "narrative_pages": [
        {
          "slug": "introduction",
          "title": "Introduction",
          "summary": "Start with the source tour and drill into linked code reference pages.",
          "concepts": ["Source Tour"],
          "modules": ["src"],
          "files": ["src/lib.rs"]
        }
      ]
    }"#
    .to_string()
}

pub(crate) fn test_concept_handbook_body() -> String {
    "## Purpose\n\nThe source tour resolves requests into repository answers [src/lib.rs:1].\n\n## How it works\n\n1. The source module anchors the concept [src/lib.rs:1].\n\n## Key components\n\n| Symbol | Role |\n| --- | --- |\n| Client | Public client [src/lib.rs:1] |\n\n## Failure modes\n\n| Signal | Response |\n| --- | --- |\n| Missing source | Inspect the linked module [src/lib.rs:1] |\n\n## How to change it\n\nUpdate the linked source and regenerate codewiki [src/lib.rs:1].\n\n## What to read next\n\nContinue to the linked module page [src/lib.rs:1].\n"
        .to_string()
}

pub(crate) fn test_narrative_handbook_body() -> String {
    "## Why this matters\n\nThe source tour is the reader's first path into the code reference [src/lib.rs:1].\n\n## How it works\n\n1. The source module anchors the walkthrough [src/lib.rs:1].\n\n## Key components\n\n| Symbol | Role |\n| --- | --- |\n| Client | Public client [src/lib.rs:1] |\n\n## Failure modes\n\n| Signal | Response |\n| --- | --- |\n| Missing source | Inspect the linked module [src/lib.rs:1] |\n\n## How to change it\n\nUpdate the linked source and regenerate codewiki [src/lib.rs:1].\n\n## What to read next\n\nContinue to the next handbook chapter [src/lib.rs:1].\n"
        .to_string()
}
