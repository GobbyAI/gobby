use super::common::parse_source;

const WORKFLOW_YAML: &str = concat!(
    "tags: [task-enforcement, enforcement, tasks, gobby, default]\n",
    "\n",
    "rules:\n",
    "  track-task-claim:\n",
    "    description: \"Log task claim events (state managed by detect_task_claim observer)\"\n",
    "    event: after_tool\n",
    "    enabled: true\n",
    "    priority: 30\n",
    "    when: \"event.data.get('mcp_tool') in ['claim_task', 'create_task'] and not event.data.get('error')\"\n",
    "    effects:\n",
    "      - type: observe\n",
    "\n",
    "  nudge-native-tracker-after-claim:\n",
    "    description: \"Define Gobby and provider-native task ownership after a successful claim\"\n",
    "    event: after_tool\n",
    "    enabled: true\n",
    "    priority: 28\n",
    "    when: >\n",
    "      event.data.get('mcp_server') == 'gobby-tasks'\n",
    "      and event.data.get('mcp_tool') in ('claim_task', 'create_task')\n",
    "      and isinstance(event.data.get('tool_output'), dict)\n",
    "      and (\n",
    "        event.data.get('tool_output', {}).get('task_id')\n",
    "          in (variables.get('claimed_tasks') or {})\n",
    "        or event.data.get('tool_output', {}).get('id')\n",
    "          in (variables.get('claimed_tasks') or {})\n",
    "        or (\n",
    "          isinstance(event.data.get('tool_output', {}).get('result'), dict)\n",
    "          and (\n",
    "            event.data.get('tool_output', {}).get('result', {}).get('task_id')\n",
    "              in (variables.get('claimed_tasks') or {})\n",
    "            or event.data.get('tool_output', {}).get('result', {}).get('id')\n",
    "              in (variables.get('claimed_tasks') or {})\n",
    "          )\n",
    "        )\n",
    "      )\n",
    "    effects:\n",
    "      - type: inject_context\n",
    "        template: >-\n",
    "          Gobby task = deliverable; provider-native tracker = implementation substeps.\n",
    "          For multi-step work, initialize the native tracker now and keep every owned\n",
    "          finding there until it is fixed before this task closes.\n",
);

#[test]
fn workflow_yaml_symbol_range_ends_on_last_included_line() {
    let parsed = parse_source("track-task-claim.yaml", WORKFLOW_YAML, &[]);
    let symbol = parsed
        .symbols
        .iter()
        .find(|symbol| symbol.name == "nudge-native-tracker-after-claim")
        .expect("workflow rule symbol");

    assert_eq!(
        symbol.byte_start,
        WORKFLOW_YAML
            .find("nudge-native-tracker-after-claim")
            .expect("rule key")
    );
    assert_eq!(symbol.byte_end, WORKFLOW_YAML.len());
    assert_eq!(symbol.line_start, 13);
    assert_eq!(symbol.line_end, 42);
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
