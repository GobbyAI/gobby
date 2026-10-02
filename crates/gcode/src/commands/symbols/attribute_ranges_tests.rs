use super::*;

#[test]
fn attribute_ranges_outline_and_symbol_by_id_agree() -> anyhow::Result<()> {
    let directory = tempfile::tempdir()?;
    let root = directory.path();
    let path = root.join("conftest.py");
    let body = "@pytest.fixture(autouse=True)\ndef fixture():\n    yield";
    std::fs::write(&path, format!("{body}\n"))?;
    let imports =
        crate::index::parser::build_import_resolution_context(root, std::slice::from_ref(&path));
    let parsed = crate::index::parser::parse_file_with_semantic(
        &path,
        "project",
        root,
        &[] as &[&str],
        &imports,
        None,
    )?
    .expect("Python fixture parses");
    assert_eq!(parsed.symbols.len(), 1);
    let symbol = &parsed.symbols[0];
    assert_eq!(symbol.signature.as_deref(), Some("def fixture():"));
    let source = read_symbol_source(root, symbol)?.expect("symbol-by-ID source exists");
    assert_eq!(source.text, body);
    for verbose in [false, true] {
        let groups = outline_groups(parsed.symbols.clone(), verbose)?;
        let json = flatten_outline_json(&groups);
        assert_eq!(json[0]["id"], symbol.id);
        assert_eq!(json[0]["line_start"], 1);
        assert_eq!(json[0]["line_end"], 3);
        assert!(
            render_outline_groups(&groups, verbose)
                .starts_with("conftest.py:1-3 [function] fixture")
        );
    }
    Ok(())
}
