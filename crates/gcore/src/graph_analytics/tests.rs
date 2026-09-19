use super::*;

fn seeded_graph() -> AnalyticsGraph {
    let nodes = ["a", "b", "c", "d", "e", "f"]
        .into_iter()
        .map(|id| AnalyticsNode {
            id: id.to_string(),
            kind: "symbol".to_string(),
            weight: if id == "c" { 5.0 } else { 1.0 },
        })
        .collect();

    let edges = [
        ("a", "b", "calls"),
        ("b", "c", "calls"),
        ("c", "a", "imports"),
        ("c", "d", "relates"),
        ("d", "e", "calls"),
        ("e", "f", "calls"),
        ("f", "d", "imports"),
    ]
    .into_iter()
    .map(|(source, target, kind)| AnalyticsEdge {
        source: source.to_string(),
        target: target.to_string(),
        kind: kind.to_string(),
        weight: weight_for_kind(kind),
    })
    .collect();

    AnalyticsGraph { nodes, edges }
}

#[test]
fn graph_analytics_detects_seeded_graph_measures() {
    let analytics = analyze(&seeded_graph());

    assert_eq!(analytics.communities.len(), 2);
    assert_eq!(
        analytics
            .communities
            .iter()
            .map(|community| community
                .nodes
                .iter()
                .map(|node| node.id.as_str())
                .collect::<Vec<_>>())
            .collect::<Vec<_>>(),
        vec![vec!["a", "b", "c"], vec!["d", "e", "f"]]
    );

    let centrality = analytics
        .centrality
        .iter()
        .map(|score| (score.node.id.as_str(), score.degree, score.score))
        .collect::<Vec<_>>();
    assert_eq!(centrality[0], ("c", 3, 0.6));
    assert_eq!(centrality[1], ("d", 3, 0.6));

    assert_eq!(
        analytics
            .bridges
            .iter()
            .map(|node| node.id.as_str())
            .collect::<Vec<_>>(),
        vec!["c", "d"]
    );
    assert_eq!(
        analytics
            .god_nodes
            .iter()
            .map(|node| node.id.as_str())
            .collect::<Vec<_>>(),
        vec!["c", "d"]
    );
    assert_eq!(
        analytics
            .unexpected_links
            .iter()
            .map(|edge| (
                edge.source.as_str(),
                edge.target.as_str(),
                edge.kind.as_str()
            ))
            .collect::<Vec<_>>(),
        vec![("c", "d", "relates")]
    );
    assert_eq!(
        analytics
            .hotspots
            .iter()
            .map(|hotspot| (hotspot.node.id.as_str(), hotspot.frequency, hotspot.weight))
            .collect::<Vec<_>>(),
        vec![("c", 3, 5.0), ("d", 3, 1.0)]
    );
}

#[test]
fn weight_for_kind_covers_observed_aliases_case_insensitively() {
    let cases = [
        ("contains", 3.0),
        ("member", 3.0),
        ("extends", 2.5),
        ("implements", 2.5),
        ("inherits", 2.5),
        ("INHERITS", 2.5),
        ("import", 2.0),
        ("imports", 2.0),
        ("IMPORTS", 2.0),
        ("call", 1.5),
        ("calls", 1.5),
        ("CALLS", 1.5),
        ("cites", 1.5),
        ("supports", 1.5),
        ("references", 1.0),
        ("uses", 1.0),
        ("callers", 1.0),
        ("links", 1.0),
        ("neighbor", 1.0),
        ("relates", 1.0),
        ("changed", 1.0),
        ("totally-unknown-kind", 1.0),
    ];
    for (kind, expected) in cases {
        assert_eq!(weight_for_kind(kind), expected, "kind={kind}");
    }
}

#[test]
fn analyze_empty_graph_does_not_panic() {
    let analytics = analyze(&AnalyticsGraph {
        nodes: Vec::new(),
        edges: Vec::new(),
    });
    assert!(analytics.communities.is_empty());
    assert!(analytics.centrality.is_empty());
    assert!(analytics.bridges.is_empty());
}

#[test]
fn analyze_nodes_without_edges_yields_singleton_communities() {
    let nodes = ["a", "b", "c"]
        .into_iter()
        .map(|id| AnalyticsNode {
            id: id.to_string(),
            kind: "symbol".to_string(),
            weight: 1.0,
        })
        .collect();
    let analytics = analyze(&AnalyticsGraph {
        nodes,
        edges: Vec::new(),
    });
    assert_eq!(analytics.communities.len(), 3);
    for community in &analytics.communities {
        assert_eq!(community.nodes.len(), 1);
    }
}

fn graph_of(nodes: &[&str], edges: &[(&str, &str, &str, f64)]) -> AnalyticsGraph {
    AnalyticsGraph {
        nodes: nodes
            .iter()
            .map(|id| AnalyticsNode {
                id: id.to_string(),
                kind: "symbol".to_string(),
                weight: 1.0,
            })
            .collect(),
        edges: edges
            .iter()
            .map(|(source, target, kind, weight)| AnalyticsEdge {
                source: source.to_string(),
                target: target.to_string(),
                kind: kind.to_string(),
                weight: *weight,
            })
            .collect(),
    }
}

#[test]
fn communities_matches_analyze_partition_on_seeded_graph() {
    let graph = seeded_graph();
    let expected = analyze(&graph).communities;
    let actual = communities(&graph).expect("seeded graph is valid input");
    assert!(!actual.is_empty());
    assert_eq!(actual, expected);
}

#[test]
fn communities_rejects_unknown_endpoint() {
    let graph = graph_of(&["a", "b"], &[("a", "z", "calls", 1.0)]);
    assert_eq!(
        communities(&graph),
        Err(GraphInputError::UnknownEndpoint {
            source: "a".to_string(),
            target: "z".to_string(),
            kind: "calls".to_string(),
        })
    );
}

#[test]
fn communities_rejects_invalid_weight() {
    for weight in [f64::NAN, 0.0] {
        let graph = graph_of(&["a", "b"], &[("a", "b", "calls", weight)]);
        match communities(&graph).expect_err("invalid weight must be rejected") {
            GraphInputError::InvalidWeight {
                source,
                target,
                kind,
                weight: seen,
            } => {
                assert_eq!(
                    (source.as_str(), target.as_str(), kind.as_str()),
                    ("a", "b", "calls")
                );
                assert_eq!(seen.to_bits(), weight.to_bits());
            }
            other => panic!("expected InvalidWeight, got {other:?}"),
        }
    }
}

#[test]
fn communities_rejects_duplicate_node() {
    let graph = graph_of(&["a", "a"], &[]);
    assert_eq!(
        communities(&graph),
        Err(GraphInputError::DuplicateNode {
            id: "a".to_string()
        })
    );
}

#[test]
fn communities_rejects_self_loop() {
    let graph = graph_of(&["a"], &[("a", "a", "calls", 1.0)]);
    assert_eq!(
        communities(&graph),
        Err(GraphInputError::SelfLoop {
            id: "a".to_string(),
            kind: "calls".to_string(),
        })
    );
}

#[test]
fn communities_empty_graph_is_empty() {
    let graph = graph_of(&[], &[]);
    assert_eq!(communities(&graph), Ok(Vec::new()));
}

#[test]
fn communities_without_edges_is_all_singletons() {
    let graph = graph_of(&["a", "b", "c"], &[]);
    let expected = analyze(&graph).communities;
    let actual = communities(&graph).expect("edge-less graph is valid input");
    assert_eq!(actual.len(), 3);
    assert!(actual.iter().all(|community| community.nodes.len() == 1));
    assert_eq!(actual, expected);
}

#[test]
fn graph_input_error_display_names_the_offending_input() {
    let cases = [
        (
            GraphInputError::DuplicateNode {
                id: "a".to_string(),
            },
            "duplicate node id a",
        ),
        (
            GraphInputError::UnknownEndpoint {
                source: "a".to_string(),
                target: "z".to_string(),
                kind: "calls".to_string(),
            },
            "edge a -> z (calls) references an unknown node",
        ),
        (
            GraphInputError::InvalidWeight {
                source: "a".to_string(),
                target: "b".to_string(),
                kind: "calls".to_string(),
                weight: 0.0,
            },
            "edge a -> b (calls) has invalid weight 0",
        ),
        (
            GraphInputError::SelfLoop {
                id: "a".to_string(),
                kind: "calls".to_string(),
            },
            "self-loop on a (calls)",
        ),
    ];
    for (error, expected) in cases {
        assert_eq!(error.to_string(), expected);
        assert!(std::error::Error::source(&error).is_none());
    }
}
