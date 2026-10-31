use std::collections::BTreeMap;

use super::{NamespaceRows, merge_namespace_rows};
use crate::graph::report::types::{GraphHotspot, GraphReportHotspots};

fn module(id: &str, degree: usize) -> GraphHotspot {
    GraphHotspot {
        id: id.to_string(),
        name: id.to_string(),
        node_type: "module".to_string(),
        degree,
        incoming: degree,
        outgoing: 0,
        file_path: None,
    }
}

fn counts(pairs: &[(&str, usize)]) -> BTreeMap<String, usize> {
    pairs
        .iter()
        .map(|(name, count)| (name.to_string(), *count))
        .collect()
}

#[test]
fn merge_ranks_a_module_shared_across_namespaces_by_combined_degree() {
    let overlay = NamespaceRows {
        node_counts_by_type: counts(&[("module", 2), ("file", 1)]),
        code_edge_counts: counts(&[("IMPORTS", 3)]),
        hotspots: GraphReportHotspots {
            high_degree_modules: vec![module("os", 2), module("serde", 3)],
            ..Default::default()
        },
        ..Default::default()
    };
    let source = NamespaceRows {
        node_counts_by_type: counts(&[("module", 1)]),
        code_edge_counts: counts(&[("IMPORTS", 2)]),
        hotspots: GraphReportHotspots {
            high_degree_modules: vec![module("os", 2)],
            ..Default::default()
        },
        ..Default::default()
    };

    let snapshot = merge_namespace_rows(&[overlay, source], 1);

    let summary = snapshot.summary.expect("summary is always present");
    assert_eq!(summary.node_count, 4);
    assert_eq!(summary.edge_count, 5);
    assert_eq!(
        summary.node_counts_by_type,
        counts(&[("file", 1), ("module", 3)])
    );

    // "os" appears in both namespaces with degree 2 each. Only its combined
    // degree (4) ranks it above serde (3), so the merge must sum before it truncates.
    let modules = snapshot.hotspots.expect("hotspots are always present");
    assert_eq!(
        modules
            .high_degree_modules
            .iter()
            .map(|row| (row.id.as_str(), row.degree))
            .collect::<Vec<_>>(),
        vec![("os", 4)]
    );
}
