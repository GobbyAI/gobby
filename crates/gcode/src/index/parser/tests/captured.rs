use std::path::Path;

use super::super::{ImportResolutionContext, parse_captured_source};

#[test]
fn captured_parser_uses_logical_identity_without_physical_root() {
    let rel_path = "captured-only/nested/identity.rs";
    assert!(!Path::new(rel_path).exists());
    let source = b"fn helper() {}\nfn caller() { helper(); }\n".to_vec();
    let context = ImportResolutionContext::default();

    let first = parse_captured_source(rel_path, "rust", "project", source.clone(), &context)
        .expect("parse first captured source")
        .expect("supported captured source");
    let second = parse_captured_source(rel_path, "rust", "project", source, &context)
        .expect("parse second captured source")
        .expect("supported captured source");

    let first_symbols = first
        .symbols
        .iter()
        .map(|symbol| (&symbol.id, &symbol.file_path))
        .collect::<Vec<_>>();
    let second_symbols = second
        .symbols
        .iter()
        .map(|symbol| (&symbol.id, &symbol.file_path))
        .collect::<Vec<_>>();
    assert_eq!(first_symbols, second_symbols);
    assert!(first_symbols.iter().all(|(_, path)| *path == rel_path));
    assert!(!first.calls.is_empty());
    let call_identity = |result: &crate::models::ParseResult| {
        result
            .calls
            .iter()
            .map(|call| {
                (
                    call.caller_symbol_id.clone(),
                    call.callee_symbol_id.clone(),
                    call.callee_name.clone(),
                    call.file_path.clone(),
                    call.content_hash.clone(),
                    call.line,
                )
            })
            .collect::<Vec<_>>()
    };
    assert_eq!(call_identity(&first), call_identity(&second));
}
