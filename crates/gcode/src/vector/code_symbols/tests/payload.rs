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
