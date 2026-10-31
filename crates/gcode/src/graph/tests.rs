use super::admitted_code_nodes;

#[test]
fn callee_placeholder_is_admitted_only_through_its_stamped_version() {
    let predicate = admitted_code_nodes("[startNode(r), endNode(r)]");

    // Native symbols keep the per-namespace check. A replaced or tombstoned
    // version of a path therefore never enters through the scope endpoint set.
    assert!(
        predicate.contains("(node.file_path + '|' + node.file_content_hash) IN $admitted_versions"),
        "native symbol versions stay checked against their own namespace"
    );
    // A placeholder has no native file props. Its stamped callee version is
    // checked against every version the scope admits, which lets a child-owned
    // caller reach a source-fresh pinned callee.
    assert!(
        predicate.contains(
            "(node.version_file_path + '|' + node.version_content_hash) IN $admitted_endpoints"
        ),
        "a callee placeholder is admitted by its stamped version in the scope endpoint set"
    );
    // Only CodeSymbol nodes are version-checked; other node classes bypass it.
    assert!(predicate.contains("NOT node:CodeSymbol"));
}

#[test]
fn query_builders_parenthesize_concatenated_version_keys_before_in() {
    // FalkorDB binds IN tighter than `+`, so `a + '|' + b IN $list` fails live
    // with "Type: mismatch: expected Boolean but was String". Every builder must
    // wrap the concatenated key: `(a + '|' + b) IN $list`.
    let builders = [
        ("graph/mod.rs", include_str!("mod.rs")),
        (
            "graph/code_graph/read/payload_queries.rs",
            include_str!("code_graph/read/payload_queries.rs"),
        ),
        (
            "graph/code_graph/read/relationship_queries.rs",
            include_str!("code_graph/read/relationship_queries.rs"),
        ),
        (
            "graph/code_graph/read/support.rs",
            include_str!("code_graph/read/support.rs"),
        ),
        ("graph/report/queries.rs", include_str!("report/queries.rs")),
    ];
    let mut offenders = Vec::new();
    for (name, source) in builders {
        for (index, line) in source.lines().enumerate() {
            if has_unparenthesized_version_key(line) {
                offenders.push(format!("{name}:{}", index + 1));
            }
        }
    }
    assert!(
        offenders.is_empty(),
        "unparenthesized '|' concatenation feeds IN in: {offenders:?}"
    );
}

fn has_unparenthesized_version_key(line: &str) -> bool {
    const CONCAT: &str = "'|' +";
    let mut rest = line;
    while let Some(at) = rest.find(CONCAT) {
        let operand = rest[at + CONCAT.len()..].trim_start();
        let end = operand
            .find(|c: char| !(c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '$')))
            .unwrap_or(operand.len());
        let after = operand[end..].trim_start();
        let feeds_in = after
            .strip_prefix("IN")
            .is_some_and(|tail| tail.starts_with(char::is_whitespace));
        if end > 0 && feeds_in {
            return true;
        }
        rest = &rest[at + CONCAT.len()..];
    }
    false
}
