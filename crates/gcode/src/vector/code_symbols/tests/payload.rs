use super::*;

#[test]
fn payloads_carry_provenance_metadata() {
    let payload = CodeSymbolVectorPayload::from_symbol(&test_symbol(Some("does work".into())));

    assert_eq!(payload.provenance, ProjectionProvenance::Extracted);
    assert_eq!(payload.confidence, Some(1.0));
    assert_eq!(payload.source_system, SOURCE_SYSTEM_GCODE);
    assert_eq!(payload.source_file_path, "src/lib.rs");
    assert_eq!(payload.source_line_start, 3);
    assert_eq!(payload.source_line_end, 5);
    assert_eq!(payload.source_byte_start, 10);
    assert_eq!(payload.source_byte_end, 40);
    assert_eq!(payload.source_line, 3);
    assert_eq!(payload.source_symbol_id, "symbol-1");
    assert_eq!(payload.summary.as_deref(), Some("does work"));
    assert_eq!(payload.signature, None);
    assert_eq!(payload.docstring, None);

    let value = serde_json::to_value(payload).expect("payload serializes");
    assert_eq!(value["provenance"], "EXTRACTED");
    assert_eq!(value["confidence"], 1.0);
    assert_eq!(value["source_system"], SOURCE_SYSTEM_GCODE);
    assert_eq!(value["source_file_path"], "src/lib.rs");
    assert_eq!(value["source_line_start"], 3);
    assert_eq!(value["source_line_end"], 5);
    assert_eq!(value["source_byte_start"], 10);
    assert_eq!(value["source_byte_end"], 40);
    assert_eq!(value["source_symbol_id"], "symbol-1");
}

#[test]
fn summaries_are_optional_enrichment() {
    let symbol = test_symbol(None);
    let payload = CodeSymbolVectorPayload::from_symbol(&symbol);
    let vector_text = vector_text_for_symbol(&symbol);
    let value = serde_json::to_value(payload).expect("payload serializes");

    assert!(value.get("summary").is_none());
    assert!(vector_text.contains("name: run"));
    assert!(!vector_text.contains("summary:"));
}

fn source_symbol(source: &str, slice: &str, kind: &str) -> (tempfile::TempDir, Symbol) {
    let dir = tempfile::tempdir().expect("source fixture directory");
    let path = dir.path().join("fixture.py");
    std::fs::write(&path, source).expect("source fixture");
    let start = source.find(slice).expect("slice occurs in fixture");
    let mut symbol = test_symbol(None);
    symbol.file_path = path.to_string_lossy().into_owned();
    symbol.language = "python".to_string();
    symbol.kind = kind.to_string();
    symbol.byte_start = start;
    symbol.byte_end = start + slice.len();
    symbol.file_content_hash = gobby_core::indexing::content_hash(source.as_bytes());
    (dir, symbol)
}

#[test]
fn vector_text_includes_exact_function_and_method_source_bytes() {
    for kind in ["function", "method"] {
        let body = "def run():\n    return 'body_only_needle_λ'\n";
        let source = format!("# preceding café\n{body}# following_secret\n");
        let (_dir, symbol) = source_symbol(&source, body, kind);

        let text = vector_text_for_symbol(&symbol);

        assert!(text.contains(body), "{kind} source slice missing: {text}");
        assert!(!text.contains("preceding café"));
        assert!(!text.contains("following_secret"));
    }
}

#[test]
fn vector_text_includes_type_and_memberless_class_source() {
    let cases = [
        (
            "type",
            "Color",
            "Color = Literal['red', 'variant_needle']\n",
        ),
        ("class", "Point", "class Point:\n    field_needle: int\n"),
    ];
    for (kind, name, body) in cases {
        let source = format!("# preceding\n{body}# following_secret\n");
        let (_dir, mut symbol) = source_symbol(&source, body, kind);
        symbol.name = name.to_string();

        let text = vector_text_for_symbol(&symbol);

        assert!(text.contains(body), "{kind} source missing: {text}");
        assert!(!text.contains("following_secret"));
    }
}

#[test]
fn vector_text_preserves_long_function_body_without_cap() {
    let body = format!(
        "def run():\n{}    return 'tail_only_needle'\n",
        "    consume('padding')\n".repeat(3000)
    );
    let (_dir, symbol) = source_symbol(&body, &body, "function");

    let text = vector_text_for_symbol(&symbol);

    assert!(
        text.contains(&body),
        "full long function must reach embedding input"
    );
}

#[test]
fn body_only_edit_changes_embedding_input_hash() {
    use std::hash::{DefaultHasher, Hash, Hasher};

    let before = "def run():\n    return 'before'\n";
    let after = "def run():\n    return 'after!'\n";
    let (_dir, symbol) = source_symbol(before, before, "function");
    let hash_input = |symbol: &Symbol| {
        let mut hasher = DefaultHasher::new();
        vector_text_for_symbol(symbol).hash(&mut hasher);
        hasher.finish()
    };
    let old_hash = hash_input(&symbol);
    std::fs::write(&symbol.file_path, after).expect("body-only edit");
    let mut changed = symbol.clone();
    changed.content_hash = "changed-body-hash".to_string();
    changed.byte_end = after.len();
    changed.file_content_hash = gobby_core::indexing::content_hash(after.as_bytes());

    assert_ne!(
        old_hash,
        hash_input(&changed),
        "body-only edit must invalidate embedding input"
    );
}

#[test]
fn container_vector_text_lists_members_without_their_bodies() {
    for (kind, signature, source) in [
        (
            "class",
            "class Worker:",
            "class Worker:\n    def execute(self):\n        return 'nested_body_secret'\n",
        ),
        (
            "module",
            "module worker",
            "def execute():\n    return 'nested_body_secret'\n",
        ),
    ] {
        let (_dir, mut symbol) = source_symbol(source, source, kind);
        symbol.name = "Worker".to_string();
        symbol.signature = Some(signature.to_string());

        let text = vector_text_for_symbol(&symbol);

        assert!(text.contains(signature));
        assert!(
            text.contains("execute"),
            "{kind} member list missing: {text}"
        );
        assert!(
            !text.contains("nested_body_secret"),
            "nested body duplicated"
        );
    }
}
