use super::*;

fn ready_plans(plans: QueryPlans) -> Vec<EdgeQueryPlan> {
    match plans {
        QueryPlans::Ready(plans) => plans,
        QueryPlans::ClosedScopeTooLarge { chunk_count } => {
            panic!("expected ready plans, got ClosedScopeTooLarge ({chunk_count} chunks)")
        }
    }
}

fn sample(
    kind: GraphEdgeKind,
    source: &str,
    target: &str,
    source_file: &str,
    target_file: &str,
) -> SampleGraphEdge {
    SampleGraphEdge {
        kind,
        source: source.to_string(),
        target: target.to_string(),
        source_file: source_file.to_string(),
        target_file: target_file.to_string(),
    }
}

fn crowded_project() -> Vec<SampleGraphEdge> {
    let mut edges = Vec::new();
    for index in 0..80 {
        edges.push(sample(
            GraphEdgeKind::Call,
            &format!("noise-src-{index}"),
            &format!("noise-dst-{index}"),
            "src/noise.rs",
            "src/other.rs",
        ));
        edges.push(sample(
            GraphEdgeKind::Import,
            "src/noise.rs",
            &format!("noise_mod_{index}"),
            "src/noise.rs",
            &format!("noise_mod_{index}"),
        ));
        edges.push(sample(
            GraphEdgeKind::Inheritance,
            &format!("noise-child-{index}"),
            &format!("noise-base-{index}"),
            "src/noise.rs",
            "src/other.rs",
        ));
    }
    edges.extend([
        sample(
            GraphEdgeKind::Call,
            "keep-caller",
            "keep-callee",
            "src/keep.rs",
            "src/keep.rs",
        ),
        sample(
            GraphEdgeKind::Import,
            "src/keep.rs",
            "keep_mod",
            "src/keep.rs",
            "keep_mod",
        ),
        sample(
            GraphEdgeKind::Inheritance,
            "keep-child",
            "keep-base",
            "src/keep.rs",
            "src/keep.rs",
        ),
    ]);
    edges
}

#[test]
fn small_scopes_keep_in_scope_edges_when_project_volume_exceeds_limit() {
    let files = ScopeKeys::Files(vec!["src/keep.rs".to_string()]);
    let edges = crowded_project();
    for kind in [
        GraphEdgeKind::Call,
        GraphEdgeKind::Import,
        GraphEdgeKind::Inheritance,
    ] {
        let mode = if kind == GraphEdgeKind::Import {
            GraphScopeMode::Incident
        } else {
            GraphScopeMode::Closed
        };
        let plan = EdgeQueryPlan::new(kind, GraphDirection::Outgoing, mode, files.clone(), 5);
        let kept = plan.select(&edges);
        assert_eq!(kept.len(), 1, "{kind:?} should retain its in-scope edge");
        assert!(
            kept.iter().all(|edge| edge.source_file == "src/keep.rs"),
            "out-of-scope {kind:?} edges must not appear"
        );
    }
}

#[test]
fn dense_scopes_return_stable_bounded_rows() {
    let mut edges = Vec::new();
    for index in 0..20 {
        edges.push(sample(
            GraphEdgeKind::Call,
            &format!("src-{index:02}"),
            &format!("dst-{index:02}"),
            "src/dense.rs",
            "src/dense.rs",
        ));
    }
    let plan = EdgeQueryPlan::new(
        GraphEdgeKind::Call,
        GraphDirection::Outgoing,
        GraphScopeMode::Closed,
        ScopeKeys::Files(vec!["src/dense.rs".to_string()]),
        7,
    );
    let first = plan
        .select(&edges)
        .into_iter()
        .map(|edge| (edge.source.clone(), edge.target.clone()))
        .collect::<Vec<_>>();
    let second = plan
        .select(&edges)
        .into_iter()
        .map(|edge| (edge.source.clone(), edge.target.clone()))
        .collect::<Vec<_>>();
    assert_eq!(first.len(), 7);
    assert_eq!(first, second);
    assert_eq!(first[0], ("src-00".to_string(), "dst-00".to_string()));
}

#[test]
fn incoming_and_outgoing_limits_are_independent() {
    let mut edges = Vec::new();
    for index in 0..6 {
        edges.push(sample(
            GraphEdgeKind::Call,
            &format!("out-{index}"),
            "seed",
            "src/out.rs",
            "src/seed.rs",
        ));
        edges.push(sample(
            GraphEdgeKind::Call,
            "seed",
            &format!("in-target-{index}"),
            "src/seed.rs",
            "src/out.rs",
        ));
    }
    let keys = ScopeKeys::Files(vec!["src/seed.rs".to_string()]);
    let outgoing = EdgeQueryPlan::new(
        GraphEdgeKind::Call,
        GraphDirection::Outgoing,
        GraphScopeMode::Incident,
        keys.clone(),
        2,
    )
    .select(&edges);
    let incoming = EdgeQueryPlan::new(
        GraphEdgeKind::Call,
        GraphDirection::Incoming,
        GraphScopeMode::Incident,
        keys,
        3,
    )
    .select(&edges);
    assert_eq!(outgoing.len(), 2);
    assert_eq!(incoming.len(), 3);
    assert!(
        outgoing
            .iter()
            .all(|edge| edge.source_file == "src/seed.rs")
    );
    assert!(
        incoming
            .iter()
            .all(|edge| { edge.target_file == "src/seed.rs" && edge.source_file != "src/seed.rs" })
    );
}

#[test]
fn query_plans_apply_scope_before_fetch_limit_and_stay_chunk_bounded() {
    let files = (0..130)
        .map(|index| format!("src/f{index}.rs"))
        .collect::<Vec<_>>();
    let plans = ready_plans(plans_for(
        GraphEdgeKind::Call,
        GraphBounds::symmetric(4),
        GraphScopeMode::Incident,
        ScopeKeys::Files(files.clone()),
    ));
    assert!(plans.len() >= 4);
    for plan in &plans {
        let (query, params) = plan.render("project-id");
        let where_at = query.find("WHERE").expect("scoped query has WHERE");
        let limit_at = query
            .find(&format!("LIMIT {}", plan.fetch_limit()))
            .expect("query uses the sentinel fetch limit");
        assert!(where_at < limit_at);
        assert!(query.contains("ORDER BY source, target"));
        assert!(query.contains("file_path IN ["));
        assert!(query.len() < 8_000);
        assert_eq!(params.len(), 1);
        match &plan.keys {
            ScopeKeys::Files(chunk) => assert!(chunk.len() <= SCOPE_CHUNK_LEN),
            other => panic!("expected file chunk, got {other:?}"),
        }
    }
    assert!(
        plans
            .iter()
            .any(|plan| plan.render("project-id").0.contains(&files[0]))
    );
}

#[test]
fn inheritance_queries_use_hierarchy_relationships() {
    let plan = EdgeQueryPlan::new(
        GraphEdgeKind::Inheritance,
        GraphDirection::Outgoing,
        GraphScopeMode::Closed,
        ScopeKeys::Symbols(vec!["child".to_string(), "base".to_string()]),
        9,
    );
    let (query, _) = plan.render("project-id");
    assert!(query.contains("INHERITS|EXTENDS|IMPLEMENTS"));
    assert!(query.contains("source.id IN ["));
    assert!(query.contains("target.id IN ["));
    assert!(query.contains("LIMIT 10"));
}

#[test]
fn closed_plans_keep_edges_whose_endpoints_fall_in_separate_chunks() {
    let files = (0..=SCOPE_CHUNK_LEN)
        .map(|index| format!("src/f{index}.rs"))
        .collect::<Vec<_>>();
    let first = files[0].clone();
    let last = files[SCOPE_CHUNK_LEN].clone();
    let plans = ready_plans(plans_for(
        GraphEdgeKind::Call,
        GraphBounds::outgoing(8),
        GraphScopeMode::Closed,
        ScopeKeys::Files(files),
    ));
    let edge = SampleGraphEdge {
        kind: GraphEdgeKind::Call,
        source: "src".to_string(),
        target: "tgt".to_string(),
        source_file: first,
        target_file: last,
    };
    assert!(
        plans
            .iter()
            .any(|plan| !plan.select(std::slice::from_ref(&edge)).is_empty()),
        "Closed plans must cover an edge whose endpoints sit in different chunks"
    );
    for plan in &plans {
        match (&plan.keys, &plan.peer_keys) {
            (ScopeKeys::Files(source), ScopeKeys::Files(target)) => {
                assert!(source.len() <= SCOPE_CHUNK_LEN);
                assert!(target.len() <= SCOPE_CHUNK_LEN);
            }
            other => panic!("expected file chunks, got {other:?}"),
        }
    }
}

#[test]
fn closed_plans_at_max_scope_stay_bounded() {
    let files = (0..SCOPE_CHUNK_LEN * MAX_CLOSED_SCOPE_CHUNKS)
        .map(|index| format!("src/f{index}.rs"))
        .collect::<Vec<_>>();
    let plans = ready_plans(plans_for(
        GraphEdgeKind::Call,
        GraphBounds::outgoing(8),
        GraphScopeMode::Closed,
        ScopeKeys::Files(files),
    ));
    let expected = MAX_CLOSED_SCOPE_CHUNKS.pow(2);
    assert_eq!(plans.len(), expected);
}

#[test]
fn closed_plans_reject_oversized_scopes_instead_of_quadratic_blowup() {
    let files = (0..SCOPE_CHUNK_LEN * MAX_CLOSED_SCOPE_CHUNKS + 1)
        .map(|index| format!("src/f{index}.rs"))
        .collect::<Vec<_>>();
    let chunk_count = files.chunks(SCOPE_CHUNK_LEN).len();
    let plans = plans_for(
        GraphEdgeKind::Call,
        GraphBounds::outgoing(8),
        GraphScopeMode::Closed,
        ScopeKeys::Files(files),
    );
    assert_eq!(plans, QueryPlans::ClosedScopeTooLarge { chunk_count });
    assert!(chunk_count > MAX_CLOSED_SCOPE_CHUNKS);
}

#[test]
fn unscoped_plans_stay_project_bounded_without_identifier_lists() {
    let plan = EdgeQueryPlan::new(
        GraphEdgeKind::Call,
        GraphDirection::Outgoing,
        GraphScopeMode::Incident,
        ScopeKeys::All,
        11,
    );
    let (query, _) = plan.render("project-id");
    assert!(!query.contains(" IN ["));
    assert!(query.contains("LIMIT 12"));
    assert!(query.contains("RETURN DISTINCT"));
}

#[test]
fn incident_incoming_excludes_cross_chunk_frontier_sources() {
    let frontier = (0..=SCOPE_CHUNK_LEN)
        .map(|index| format!("sym-{index:03}"))
        .collect::<Vec<_>>();
    let mut edges = Vec::new();
    for index in 0..6 {
        edges.push(sample(
            GraphEdgeKind::Call,
            &frontier[SCOPE_CHUNK_LEN],
            &frontier[index],
            &frontier[SCOPE_CHUNK_LEN],
            &frontier[index],
        ));
    }
    edges.push(sample(
        GraphEdgeKind::Call,
        "external-a",
        &frontier[0],
        "src/ext.rs",
        &frontier[0],
    ));
    edges.push(sample(
        GraphEdgeKind::Call,
        "external-b",
        &frontier[0],
        "src/ext.rs",
        &frontier[0],
    ));
    let keys = ScopeKeys::Symbols(frontier);
    let (rows, incoming_truncated, outgoing_truncated) = collect_scoped_pairs(
        &edges,
        GraphEdgeKind::Call,
        GraphBounds {
            incoming_limit: 2,
            outgoing_limit: 0,
        },
        GraphScopeMode::Incident,
        keys,
        &HashSet::new(),
    );
    assert!(!outgoing_truncated);
    assert!(!incoming_truncated);
    assert_eq!(rows.len(), 2);
    assert!(
        rows.iter()
            .all(|edge| edge.source.starts_with("external-") && edge.rel == "CALLS")
    );
    let plans = ready_plans(plans_for(
        GraphEdgeKind::Call,
        GraphBounds {
            incoming_limit: 2,
            outgoing_limit: 0,
        },
        GraphScopeMode::Incident,
        ScopeKeys::Symbols(
            (0..=SCOPE_CHUNK_LEN)
                .map(|index| format!("sym-{index:03}"))
                .collect(),
        ),
    ));
    let incoming = plans
        .iter()
        .filter(|plan| plan.direction == GraphDirection::Incoming)
        .collect::<Vec<_>>();
    assert!(incoming.len() >= 2);
    assert!(
        incoming
            .iter()
            .any(|plan| !plan.render("project-id").0.contains("NOT "))
    );
}

#[test]
fn scoped_limits_count_distinct_public_edges() {
    let mut edges = Vec::new();
    for _ in 0..8 {
        edges.push(sample(
            GraphEdgeKind::Call,
            "dup-src",
            "dup-dst",
            "src/a.rs",
            "src/b.rs",
        ));
    }
    edges.push(sample(
        GraphEdgeKind::Call,
        "other-src",
        "other-dst",
        "src/c.rs",
        "src/d.rs",
    ));
    let (rows, incoming_truncated, outgoing_truncated) = collect_scoped_pairs(
        &edges,
        GraphEdgeKind::Call,
        GraphBounds::outgoing(2),
        GraphScopeMode::Incident,
        ScopeKeys::Symbols(vec!["dup-src".into(), "other-src".into()]),
        &HashSet::new(),
    );
    assert!(!incoming_truncated);
    assert!(!outgoing_truncated);
    assert_eq!(rows.len(), 2);
    let query = EdgeQueryPlan::new(
        GraphEdgeKind::Call,
        GraphDirection::Outgoing,
        GraphScopeMode::Incident,
        ScopeKeys::Symbols(vec!["dup-src".into()]),
        2,
    )
    .render("project-id")
    .0;
    assert!(query.contains("RETURN DISTINCT"));
    assert!(query.contains("type(r) AS rel"));
}

#[test]
fn scoped_limits_exclude_already_emitted_edges() {
    let edges = vec![
        sample(
            GraphEdgeKind::Call,
            "aaa-src",
            "seed",
            "src/a.rs",
            "src/seed.rs",
        ),
        sample(
            GraphEdgeKind::Call,
            "bbb-src",
            "seed",
            "src/b.rs",
            "src/seed.rs",
        ),
    ];
    let exclude = HashSet::from([PublicEdge::new("aaa-src", "seed", "CALLS")]);
    let (rows, incoming_truncated, _) = collect_scoped_pairs(
        &edges,
        GraphEdgeKind::Call,
        GraphBounds {
            incoming_limit: 1,
            outgoing_limit: 0,
        },
        GraphScopeMode::Incident,
        ScopeKeys::Files(vec!["src/seed.rs".into()]),
        &exclude,
    );
    assert_eq!(rows, vec![PublicEdge::new("bbb-src", "seed", "CALLS")]);
    assert!(!incoming_truncated);
}

#[test]
fn scoped_limits_exclude_already_emitted_outgoing_edges() {
    let edges = vec![
        sample(
            GraphEdgeKind::Call,
            "seed",
            "aaa-dst",
            "src/seed.rs",
            "src/a.rs",
        ),
        sample(
            GraphEdgeKind::Call,
            "seed",
            "bbb-dst",
            "src/seed.rs",
            "src/b.rs",
        ),
    ];
    let exclude = HashSet::from([PublicEdge::new("seed", "aaa-dst", "CALLS")]);
    let (rows, _, outgoing_truncated) = collect_scoped_pairs(
        &edges,
        GraphEdgeKind::Call,
        GraphBounds::outgoing(1),
        GraphScopeMode::Incident,
        ScopeKeys::Files(vec!["src/seed.rs".into()]),
        &exclude,
    );
    assert_eq!(rows, vec![PublicEdge::new("seed", "bbb-dst", "CALLS")]);
    assert!(!outgoing_truncated);
}
