use super::support::*;
use super::*;

#[test]
fn frontmatter_provenance_accepts_unquoted_and_escaped_values() {
    let files = source_files_from_frontmatter(
        r#"---
provenance:
  - file: src/plain.rs
  - file: "src/escaped\"quote.rs"
---
"#,
    );

    assert!(files.contains("src/plain.rs"));
    assert!(files.contains("src/escaped\"quote.rs"));
}

#[test]
fn frontmatter_provenance_parse_yaml_with_ranges() {
    let files = source_files_from_frontmatter(
        r#"---
title: "Example"
provenance:
  - file: "src/one:thing.rs"
    ranges:
      - "1-4"
  - file: src/two.rs
---
"#,
    );

    assert!(files.contains("src/one:thing.rs"));
    assert!(files.contains("src/two.rs"));
    assert_eq!(files.len(), 2);
}

#[test]
fn frontmatter_legacy_source_files_are_ignored() {
    let files = source_files_from_frontmatter(
        r#"---
source_files:
- file: src/legacy.rs
sources:
- file: src/also-legacy.rs
---
"#,
    );

    assert!(files.is_empty());
}

#[test]
fn source_hashes_reject_frontmatter_paths_outside_project_root() {
    let tempdir = tempfile::tempdir().expect("tempdir");
    let project_root = tempdir.path().join("project");
    std::fs::create_dir_all(&project_root).expect("project root");
    std::fs::write(tempdir.path().join("outside.rs"), "fn outside() {}").expect("outside file");
    let content = r#"---
provenance:
  - file: ../outside.rs
---
"#;

    let err = source_hashes_for_doc(&project_root, content).expect_err("outside source rejected");

    assert!(
        err.to_string().contains("resolves outside project root"),
        "unexpected error: {err}"
    );
}

#[test]
fn yaml_unquote_translates_common_escapes_and_rejects_incomplete_escape() {
    assert_eq!(
        unquote_yaml_string(r#""line\nquote\"tab\tbackslash\\""#),
        Some("line\nquote\"tab\tbackslash\\".to_string())
    );
    assert_eq!(
        unquote_yaml_string(r#""hex\x21 unicode\u2713 scalar\U0001F680""#),
        Some("hex! unicode\u{2713} scalar\u{1f680}".to_string())
    );
    let incomplete = format!("\"{}\\\"", "src/incomplete");
    assert_eq!(unquote_yaml_string(&incomplete), None);
    assert_eq!(unquote_yaml_string(r#""bad\x1""#), None);
    assert_eq!(unquote_yaml_string(r#""bad\u12xz""#), None);
    assert_eq!(unquote_yaml_string(r#""bad\U00110000""#), None);
}

#[test]
fn frontmatter_serializes_scalars_with_serde_yaml() {
    let source_file = "src/quote\"colon:thing.rs";
    let doc = frontmatter(
        "line\nquote\"tab\tbackslash\\nul\0bell\u{0007}",
        "code_file",
        &[SourceSpan {
            file: source_file.to_string(),
            line_start: 7,
            line_end: 9,
        }],
    );
    let yaml = doc
        .strip_prefix("---\n")
        .and_then(|content| content.strip_suffix("---\n\n"))
        .expect("frontmatter delimiters");
    let parsed: serde_yaml::Value = serde_yaml::from_str(yaml).expect("frontmatter parses");
    let serde_yaml::Value::Mapping(mapping) = parsed else {
        panic!("frontmatter is a YAML mapping");
    };

    assert_eq!(
        mapping
            .get(serde_yaml::Value::String("title".to_string()))
            .and_then(serde_yaml::Value::as_str),
        Some("line\nquote\"tab\tbackslash\\nul\0bell\u{0007}")
    );
    assert_eq!(
        mapping
            .get(serde_yaml::Value::String("type".to_string()))
            .and_then(serde_yaml::Value::as_str),
        Some("code_file")
    );
    assert!(source_files_from_frontmatter(&doc).contains(source_file));
}

#[test]
fn citations_validated_against_spans() {
    let input = CodewikiInput {
        files: vec!["src/lib.rs".to_string()],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![
            test_symbol_range(
                "src/lib.rs",
                "Client",
                "class",
                10,
                14,
                "pub struct Client {",
            ),
            test_symbol_range(
                "src/lib.rs",
                "connect",
                "function",
                20,
                24,
                "pub fn connect()",
            ),
        ],
    };
    let mut generator = |prompt: &str, _system: &str| {
        if prompt.contains("Client") {
            Some("Builds client state [src/lib.rs:999].".to_string())
        } else if prompt.contains("connect") {
            Some("Opens a connection [src/lib.rs:20].".to_string())
        } else {
            Some("Coordinates the public API [missing.rs:1].".to_string())
        }
    };

    let docs = generate_hierarchical_docs(&input, Some(&mut generator));
    let file_doc = docs
        .iter()
        .find(|(path, _)| path == "code/files/src/lib.rs.md")
        .map(|(_, content)| content)
        .expect("file doc");

    assert!(!file_doc.contains("source:\n"));
    assert!(file_doc.contains("provenance:\n"));
    assert!(source_files_from_frontmatter(file_doc).contains("src/lib.rs"));
    assert!(file_doc.contains("10-14"));
    assert!(file_doc.contains("20-24"));
    assert!(file_doc.contains("[src/lib.rs:10-14]"));
    assert!(file_doc.contains("[src/lib.rs:20]"));
    assert!(!file_doc.contains("src/lib.rs:999"));
    assert!(!file_doc.contains("missing.rs:1"));
}
