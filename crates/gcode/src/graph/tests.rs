use super::admitted_code_nodes;

#[test]
fn callee_placeholder_is_admitted_only_through_its_stamped_version() {
    let predicate = admitted_code_nodes("[startNode(r), endNode(r)]");

    // Native symbols keep the per-namespace check. A replaced or tombstoned
    // version of a path therefore never enters through the scope endpoint set.
    assert!(
        predicate.contains("node.file_path + '|' + node.file_content_hash IN $admitted_versions"),
        "native symbol versions stay checked against their own namespace"
    );
    // A placeholder has no native file props. Its stamped callee version is
    // checked against every version the scope admits, which lets a child-owned
    // caller reach a source-fresh pinned callee.
    assert!(
        predicate.contains(
            "node.version_file_path + '|' + node.version_content_hash IN $admitted_endpoints"
        ),
        "a callee placeholder is admitted by its stamped version in the scope endpoint set"
    );
    // Only CodeSymbol nodes are version-checked; other node classes bypass it.
    assert!(predicate.contains("NOT node:CodeSymbol"));
}
