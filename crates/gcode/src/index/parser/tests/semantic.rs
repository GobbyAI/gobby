use std::fs;
use std::path::PathBuf;

use tempfile::TempDir;

use crate::index::semantic::{
    SemanticCallRequest, SemanticCallResolver, SemanticCallTarget, SemanticTargetKind,
};

use super::super::{build_import_resolution_context, parse_file_with_semantic};
use super::common::discover_supported_files;

struct FakeSemanticResolver {
    target: Option<crate::index::semantic::SemanticCallTarget>,
    expected_language: &'static str,
    expected_callee: &'static str,
    requests: Vec<CapturedSemanticRequest>,
    error: Option<&'static str>,
}

struct CapturedSemanticRequest {
    language: String,
    file_path: PathBuf,
    root_path: PathBuf,
    callee_name: String,
    line: usize,
    column: usize,
}

impl SemanticCallResolver for FakeSemanticResolver {
    fn resolve(
        &mut self,
        request: &SemanticCallRequest<'_>,
    ) -> anyhow::Result<Option<crate::index::semantic::SemanticCallTarget>> {
        self.requests.push(CapturedSemanticRequest {
            language: request.language.to_string(),
            file_path: request.file_path.to_path_buf(),
            root_path: request.root_path.to_path_buf(),
            callee_name: request.callee_name.to_string(),
            line: request.line,
            column: request.column,
        });
        if let Some(error) = self.error {
            anyhow::bail!("{error}");
        }
        if request.language == self.expected_language && request.callee_name == self.expected_callee
        {
            Ok(self.target.clone())
        } else {
            Ok(None)
        }
    }
}

#[test]
fn semantic_resolver_can_classify_cpp_calls_as_external() {
    let tempdir = TempDir::new().expect("create tempdir");
    let root = tempdir.path();
    let path = root.join("src/main.cpp");
    fs::create_dir_all(path.parent().expect("parent")).expect("create parent dirs");
    fs::write(
        &path,
        r#"
void run() {
    printf("x");
}
"#,
    )
    .expect("write source");
    let candidates = discover_supported_files(root);
    let context = build_import_resolution_context(root, &candidates);
    let mut resolver = FakeSemanticResolver {
        target: Some(SemanticCallTarget {
            callee_name: "printf".to_string(),
            kind: SemanticTargetKind::External("/usr/include/stdio.h".to_string()),
        }),
        expected_language: "cpp",
        expected_callee: "printf",
        requests: Vec::new(),
        error: None,
    };
    let parsed = parse_file_with_semantic(
        &path,
        "proj",
        root,
        &[] as &[&str],
        &context,
        Some(&mut resolver),
    )
    .expect("parse result")
    .expect("parse file");

    let call = parsed.calls.first().expect("printf call");
    assert_eq!(call.callee_target_kind.as_str(), "external");
    assert_eq!(
        call.callee_external_module.as_deref(),
        Some("/usr/include/stdio.h")
    );
}

#[test]
fn semantic_resolver_can_classify_cpp_calls_as_local_import() {
    let tempdir = TempDir::new().expect("create tempdir");
    let root = tempdir.path();
    let path = root.join("src/main.cpp");
    fs::create_dir_all(path.parent().expect("parent")).expect("create parent dirs");
    fs::write(
        &path,
        r#"
void run() {
    helper();
}
"#,
    )
    .expect("write source");
    let candidates = discover_supported_files(root);
    let context = build_import_resolution_context(root, &candidates);
    // clangd resolved `helper` to a definition INSIDE the project root. The
    // resolver hands back the project-relative candidate file; materialization
    // must route it through the local-import candidate path (not external) so
    // the post-write DB pass can narrow it to a canonical symbol id.
    let mut resolver = FakeSemanticResolver {
        target: Some(SemanticCallTarget {
            callee_name: "helper".to_string(),
            kind: SemanticTargetKind::LocalCandidate("src/util.cpp".to_string()),
        }),
        expected_language: "cpp",
        expected_callee: "helper",
        requests: Vec::new(),
        error: None,
    };
    let parsed = parse_file_with_semantic(
        &path,
        "proj",
        root,
        &[] as &[&str],
        &context,
        Some(&mut resolver),
    )
    .expect("parse result")
    .expect("parse file");

    let call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "helper")
        .expect("helper call");
    assert_eq!(call.callee_target_kind.as_str(), "local_import");
    assert_eq!(
        call.local_import_candidate_files(),
        vec!["src/util.cpp".to_string()]
    );
}

#[test]
fn semantic_resolver_can_classify_textual_dart_calls_as_external() {
    let tempdir = TempDir::new().expect("create tempdir");
    let root = tempdir.path();
    let path = root.join("lib/sample.dart");
    fs::create_dir_all(path.parent().expect("parent")).expect("create parent dirs");
    fs::write(
        &path,
        r#"
void run() {
  Tooltip(message: 'x');
}
"#,
    )
    .expect("write source");
    let candidates = discover_supported_files(root);
    let context = build_import_resolution_context(root, &candidates);
    let mut resolver = FakeSemanticResolver {
        target: Some(SemanticCallTarget {
            callee_name: "Tooltip".to_string(),
            kind: SemanticTargetKind::External("package:flutter/material.dart".to_string()),
        }),
        expected_language: "dart",
        expected_callee: "Tooltip",
        requests: Vec::new(),
        error: None,
    };
    let parsed = parse_file_with_semantic(
        &path,
        "proj",
        root,
        &[] as &[&str],
        &context,
        Some(&mut resolver),
    )
    .expect("parse result")
    .expect("parse file");

    let call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "Tooltip")
        .expect("Tooltip call");
    assert_eq!(call.callee_target_kind.as_str(), "external");
    assert_eq!(
        call.callee_external_module.as_deref(),
        Some("package:flutter/material.dart")
    );
    assert!(resolver.requests.iter().any(|request| {
        request.language == "dart"
            && request.file_path == path
            && request.root_path == root
            && request.callee_name == "Tooltip"
    }));
}

#[test]
fn semantic_resolver_receives_utf16_columns_for_ast_calls() {
    let tempdir = TempDir::new().expect("create tempdir");
    let root = tempdir.path();
    let path = root.join("src/main.cpp");
    fs::create_dir_all(path.parent().expect("parent")).expect("create parent dirs");
    let source = format!(
        "void run() {{\n    auto s = \"{}\"; printf(\"x\");\n}}\n",
        '\u{1F600}'
    );
    fs::write(&path, source.as_bytes()).expect("write source");
    let candidates = discover_supported_files(root);
    let context = build_import_resolution_context(root, &candidates);
    let mut resolver = FakeSemanticResolver {
        target: None,
        expected_language: "cpp",
        expected_callee: "printf",
        requests: Vec::new(),
        error: None,
    };

    parse_file_with_semantic(
        &path,
        "proj",
        root,
        &[] as &[&str],
        &context,
        Some(&mut resolver),
    )
    .expect("parse result")
    .expect("parse file");

    let request = resolver
        .requests
        .iter()
        .find(|request| request.callee_name == "printf")
        .expect("printf semantic request");
    let prefix = format!("    auto s = \"{}\"; ", '\u{1F600}');
    assert_eq!(request.line, 2);
    assert_eq!(request.column, prefix.encode_utf16().count());
}

#[test]
fn semantic_resolver_receives_utf16_columns_for_textual_dart_calls() {
    let tempdir = TempDir::new().expect("create tempdir");
    let root = tempdir.path();
    let path = root.join("lib/sample.dart");
    fs::create_dir_all(path.parent().expect("parent")).expect("create parent dirs");
    let source = format!(
        "void run() {{\n  final s = '{}'; Tooltip(message: 'x');\n}}\n",
        '\u{1F600}'
    );
    fs::write(&path, source.as_bytes()).expect("write source");
    let candidates = discover_supported_files(root);
    let context = build_import_resolution_context(root, &candidates);
    let mut resolver = FakeSemanticResolver {
        target: None,
        expected_language: "dart",
        expected_callee: "Tooltip",
        requests: Vec::new(),
        error: None,
    };

    parse_file_with_semantic(
        &path,
        "proj",
        root,
        &[] as &[&str],
        &context,
        Some(&mut resolver),
    )
    .expect("parse result")
    .expect("parse file");

    let request = resolver
        .requests
        .iter()
        .find(|request| request.callee_name == "Tooltip")
        .expect("Tooltip semantic request");
    let prefix = format!("  final s = '{}'; ", '\u{1F600}');
    assert_eq!(request.line, 2);
    assert_eq!(request.column, prefix.encode_utf16().count());
}

#[test]
fn semantic_resolver_receives_dart_byte_offsets_across_crlf_lines() {
    let tempdir = TempDir::new().expect("create tempdir");
    let root = tempdir.path();
    let path = root.join("lib/sample.dart");
    fs::create_dir_all(path.parent().expect("parent")).expect("create parent dirs");
    let source = format!(
        "void run() {{\r\n  final s = '{}';\r\n  Tooltip(message: 'x');\r\n}}\r\n",
        '\u{1F600}'
    );
    fs::write(&path, source.as_bytes()).expect("write source");
    let candidates = discover_supported_files(root);
    let context = build_import_resolution_context(root, &candidates);
    let mut resolver = FakeSemanticResolver {
        target: None,
        expected_language: "dart",
        expected_callee: "Tooltip",
        requests: Vec::new(),
        error: None,
    };

    parse_file_with_semantic(
        &path,
        "proj",
        root,
        &[] as &[&str],
        &context,
        Some(&mut resolver),
    )
    .expect("parse result")
    .expect("parse file");

    let request = resolver
        .requests
        .iter()
        .find(|request| request.callee_name == "Tooltip")
        .expect("Tooltip semantic request");
    assert_eq!(request.line, 3);
    assert_eq!(request.column, 2);
}

#[test]
fn semantic_resolver_errors_are_propagated() {
    let tempdir = TempDir::new().expect("create tempdir");
    let root = tempdir.path();
    let path = root.join("src/main.cpp");
    fs::create_dir_all(path.parent().expect("parent")).expect("create parent dirs");
    fs::write(
        &path,
        r#"
void run() {
    printf("x");
}
"#,
    )
    .expect("write source");
    let candidates = discover_supported_files(root);
    let context = build_import_resolution_context(root, &candidates);
    let mut resolver = FakeSemanticResolver {
        target: None,
        expected_language: "cpp",
        expected_callee: "printf",
        requests: Vec::new(),
        error: Some("semantic resolver failed"),
    };

    let err = match parse_file_with_semantic(
        &path,
        "proj",
        root,
        &[] as &[&str],
        &context,
        Some(&mut resolver),
    ) {
        Err(err) => err,
        Ok(_) => panic!("expected semantic resolver error"),
    };

    assert_eq!(err.to_string(), "semantic resolver failed");
}
