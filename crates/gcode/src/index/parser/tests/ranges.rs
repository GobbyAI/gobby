use super::common::parse_source;

const WORKFLOW_YAML: &str = concat!(
    "rules:\n",
    "  fixture-observer:\n",
    "    event: after_tool\n",
    "    effects: []\n",
    "\n",
    "  fixture-folded-rule:\n",
    "    event: before_tool\n",
    "    when: >\n",
    "      variables.get('fixture_ready')\n",
    "      and not event.data.get('error')\n",
    "    effects:\n",
    "      - type: inject_context\n",
    "        template: >-\n",
    "          First fixture line.\n",
    "          Second fixture line.\n",
    "          Final fixture line.\n",
);

#[test]
fn workflow_yaml_symbol_range_ends_on_last_included_line() {
    let parsed = parse_source("fixture-ranges.yaml", WORKFLOW_YAML, &[]);
    let symbol = parsed
        .symbols
        .iter()
        .find(|symbol| symbol.name == "fixture-folded-rule")
        .expect("workflow rule symbol");

    assert_eq!(
        symbol.byte_start,
        WORKFLOW_YAML.find("fixture-folded-rule").expect("rule key")
    );
    assert_eq!(symbol.byte_end, WORKFLOW_YAML.len());
    assert_eq!(symbol.line_start, 6);
    assert_eq!(symbol.line_end, 16);
}

#[test]
fn parser_symbol_ranges_match_inclusive_blob_bytes_across_source_and_data_shapes() {
    let cases = [
        ("source one-line EOF", "sample.py", "def one(): pass"),
        ("source one-line LF", "sample.py", "def one(): pass\n"),
        (
            "source multiline EOF",
            "sample.py",
            "def many():\n    value = 1\n    return value",
        ),
        (
            "source CRLF and real blank line",
            "sample.py",
            "def many():\r\n    value = 1\r\n\r\n    return value\r\n",
        ),
        (
            "source next definition at column zero",
            "sample.py",
            "def first():\n    pass\ndef second():\n    pass\n",
        ),
        ("data one-line EOF", "config.json", r#"{"one": 1}"#),
        ("data one-line LF", "config.yaml", "one: value\n"),
        (
            "data multiline EOF",
            "config.json",
            "{\n  \"one\": {\n    \"two\": 2\n  }\n}",
        ),
        (
            "data CRLF and real blank line",
            "config.yaml",
            "one: |-\r\n  first\r\n\r\n  last\r\n",
        ),
        (
            "data next key at column zero",
            "config.yaml",
            "one: |-\n  value\ntwo: value\n",
        ),
        ("workflow YAML terminal LF", "workflow.yaml", WORKFLOW_YAML),
    ];

    for (case, file_name, source) in cases {
        let parsed = parse_source(file_name, source, &[]);
        assert!(!parsed.symbols.is_empty(), "{case}: expected symbols");
        for symbol in parsed.symbols {
            assert!(
                symbol.byte_start < symbol.byte_end,
                "{case}: {} has an empty byte range",
                symbol.name
            );
            assert_eq!(
                symbol.line_start,
                line_at(source.as_bytes(), symbol.byte_start),
                "{case}: {} start",
                symbol.name
            );
            assert_eq!(
                symbol.line_end,
                line_at(source.as_bytes(), symbol.byte_end - 1),
                "{case}: {} end",
                symbol.name
            );
        }
    }
}

fn line_at(source: &[u8], offset: usize) -> usize {
    source[..offset]
        .iter()
        .filter(|byte| **byte == b'\n')
        .count()
        + 1
}
