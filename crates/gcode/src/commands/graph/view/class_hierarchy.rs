//! Class-hierarchy walker for `gcode graph view --view=class-hierarchy`.

mod fetch;

use std::collections::{BTreeMap, HashSet};

use crate::codewiki_facts::PublicEdge;

use super::render::{NodeKey, ViewEdgeInput, ViewNodeInput};
use super::{
    CandidateEndpoint, CandidateEndpointKind, ViewEdgeCandidate, VisibleFileMap,
    take_visible_before_bound,
};

pub(crate) use fetch::run;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum HeritageDirection {
    Ancestors,
    Descendants,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(super) struct HeritageCursor {
    pub source: String,
    pub target: String,
    pub rel: String,
    pub edge_id: i64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(super) struct HeritageHopRow {
    pub source: CandidateEndpoint,
    pub target: CandidateEndpoint,
    pub rel: String,
    pub edge_id: i64,
    pub owner_path: String,
    pub owner_hash: String,
    pub owner_machine: String,
    pub overlay_shadowed: bool,
}

impl HeritageHopRow {
    #[cfg(test)]
    fn order_key(&self) -> (&str, &str, &str, i64) {
        (
            self.source.id.as_str(),
            self.target.id.as_str(),
            self.rel.as_str(),
            self.edge_id,
        )
    }

    #[cfg(test)]
    fn is_after(&self, cursor: &HeritageCursor) -> bool {
        self.order_key()
            > (
                cursor.source.as_str(),
                cursor.target.as_str(),
                cursor.rel.as_str(),
                cursor.edge_id,
            )
    }

    fn cursor(&self) -> HeritageCursor {
        HeritageCursor {
            source: self.source.id.clone(),
            target: self.target.id.clone(),
            rel: self.rel.clone(),
            edge_id: self.edge_id,
        }
    }

    fn to_candidate(&self, hop: usize) -> ViewEdgeCandidate {
        ViewEdgeCandidate {
            source: self.source.clone(),
            target: self.target.clone(),
            rel: self.rel.clone(),
            owner_path: self.owner_path.clone(),
            owner_hash: self.owner_hash.clone(),
            owner_machine: self.owner_machine.clone(),
            overlay_shadowed: self.overlay_shadowed,
            hop,
        }
    }
}

pub(super) struct ChgWalk {
    pub nodes: Vec<ViewNodeInput>,
    pub edges: Vec<ViewEdgeInput>,
    pub incoming_truncated: bool,
    pub outgoing_truncated: bool,
}

struct ChgAcc {
    nodes: BTreeMap<NodeKey, ViewNodeInput>,
    edges: Vec<ViewEdgeInput>,
    emitted: HashSet<PublicEdge>,
}

pub(super) fn exhaust_heritage_hop<F>(
    mut fetch_page: F,
    page_size: usize,
) -> anyhow::Result<Vec<HeritageHopRow>>
where
    F: FnMut(Option<&HeritageCursor>) -> anyhow::Result<Vec<HeritageHopRow>>,
{
    let page_size = page_size.max(1);
    let mut collected = Vec::new();
    let mut cursor = None;
    loop {
        let mut page = fetch_page(cursor.as_ref())?;
        if page.len() > page_size {
            page.truncate(page_size);
        }
        if page.is_empty() {
            break;
        }
        let raw_count = page.len();
        let next = page.last().map(HeritageHopRow::cursor);
        if next.as_ref() == cursor.as_ref() {
            break;
        }
        collected.extend(page);
        if raw_count < page_size {
            break;
        }
        cursor = next;
    }
    Ok(collected)
}

fn public_edge(edge: &ViewEdgeCandidate) -> PublicEdge {
    PublicEdge::new(
        endpoint_key(&edge.source).canonical(),
        endpoint_key(&edge.target).canonical(),
        &edge.rel,
    )
}

fn neighbor(edge: &ViewEdgeCandidate, direction: HeritageDirection) -> &CandidateEndpoint {
    match direction {
        HeritageDirection::Ancestors => &edge.target,
        HeritageDirection::Descendants => &edge.source,
    }
}

fn walk_directed(
    seed: &CandidateEndpoint,
    depth: u32,
    direction: HeritageDirection,
    acc: &mut ChgAcc,
    visible_of: &mut impl FnMut(&[ViewEdgeCandidate]) -> anyhow::Result<VisibleFileMap>,
    fetch: &mut impl FnMut(
        &[CandidateEndpoint],
        HeritageDirection,
    ) -> anyhow::Result<Vec<HeritageHopRow>>,
) -> anyhow::Result<bool> {
    let mut visited = HashSet::from([endpoint_key(seed)]);
    let mut frontier = vec![seed.clone()];
    for hop in 1..=depth {
        if frontier.is_empty() {
            break;
        }
        let rows = fetch(&frontier, direction)?;
        let candidates = rows
            .iter()
            .map(|row| row.to_candidate(hop as usize))
            .collect::<Vec<_>>();
        let visible = visible_of(&candidates)?;
        let kept = take_visible_before_bound(candidates, &visible, None, None);
        let mut next_frontier = Vec::new();
        for edge in kept.edges {
            if !acc.emitted.insert(public_edge(&edge)) {
                continue;
            }
            acc.nodes
                .entry(endpoint_key(&edge.source))
                .or_insert_with(|| node_from_endpoint(&edge.source));
            acc.nodes
                .entry(endpoint_key(&edge.target))
                .or_insert_with(|| node_from_endpoint(&edge.target));
            acc.edges.push(ViewEdgeInput {
                source: endpoint_key(&edge.source),
                target: endpoint_key(&edge.target),
                rel: edge.rel.clone(),
            });
            let next = neighbor(&edge, direction);
            if expandable(next.kind) && visited.insert(endpoint_key(next)) {
                next_frontier.push(next.clone());
            }
        }
        frontier = next_frontier;
    }
    if frontier.is_empty() {
        return Ok(false);
    }
    let peek_hop = depth.saturating_add(1) as usize;
    let rows = fetch(&frontier, direction)?;
    let candidates = rows
        .iter()
        .map(|row| row.to_candidate(peek_hop))
        .collect::<Vec<_>>();
    let visible = visible_of(&candidates)?;
    let kept = take_visible_before_bound(candidates, &visible, None, None);
    Ok(kept
        .edges
        .iter()
        .any(|edge| !acc.emitted.contains(&public_edge(edge))))
}

pub(super) fn walk_chg(
    seed: CandidateEndpoint,
    depth: u32,
    mut visible_of: impl FnMut(&[ViewEdgeCandidate]) -> anyhow::Result<VisibleFileMap>,
    mut fetch: impl FnMut(
        &[CandidateEndpoint],
        HeritageDirection,
    ) -> anyhow::Result<Vec<HeritageHopRow>>,
) -> anyhow::Result<ChgWalk> {
    let mut acc = ChgAcc {
        nodes: BTreeMap::from([(endpoint_key(&seed), node_from_endpoint(&seed))]),
        edges: Vec::new(),
        emitted: HashSet::new(),
    };
    let outgoing_truncated = walk_directed(
        &seed,
        depth,
        HeritageDirection::Ancestors,
        &mut acc,
        &mut visible_of,
        &mut fetch,
    )?;
    let incoming_truncated = walk_directed(
        &seed,
        depth,
        HeritageDirection::Descendants,
        &mut acc,
        &mut visible_of,
        &mut fetch,
    )?;
    Ok(ChgWalk {
        nodes: acc.nodes.into_values().collect(),
        edges: acc.edges,
        incoming_truncated,
        outgoing_truncated,
    })
}

fn expandable(kind: CandidateEndpointKind) -> bool {
    matches!(kind, CandidateEndpointKind::Symbol)
}

fn endpoint_key(endpoint: &CandidateEndpoint) -> NodeKey {
    match endpoint.kind {
        CandidateEndpointKind::Symbol => NodeKey::symbol(&endpoint.id),
        CandidateEndpointKind::File => NodeKey::file(&endpoint.id),
        CandidateEndpointKind::Module => NodeKey::module(&endpoint.id),
        CandidateEndpointKind::External => NodeKey::external(&endpoint.id),
        CandidateEndpointKind::Unresolved => NodeKey::unresolved(&endpoint.id),
    }
}

fn node_from_endpoint(endpoint: &CandidateEndpoint) -> ViewNodeInput {
    ViewNodeInput {
        key: endpoint_key(endpoint),
        name: endpoint.name.clone().unwrap_or_else(|| endpoint.id.clone()),
        kind: match endpoint.kind {
            CandidateEndpointKind::Symbol => "symbol".to_string(),
            CandidateEndpointKind::File => "file".to_string(),
            CandidateEndpointKind::Module => "module".to_string(),
            CandidateEndpointKind::External => "external".to_string(),
            CandidateEndpointKind::Unresolved => "unresolved".to_string(),
        },
        file: endpoint.file.clone(),
        community: None,
    }
}

#[cfg(test)]
mod tests {
    use std::collections::HashSet;

    use crate::cli::GraphViewKind;
    use crate::codewiki_facts::MAX_DECLARED_EDGE_LIMIT;
    use crate::commands::graph::view::render::build_view_payload;
    use crate::commands::graph::view::{
        CandidateEndpoint, CandidateEndpointKind, VisibleFileMap, VisibleOwnerKey,
    };

    use super::fetch::heritage_hop_query;
    use super::{
        ChgWalk, HeritageCursor, HeritageDirection, HeritageHopRow, exhaust_heritage_hop, walk_chg,
    };

    const MACHINE: &str = "machine-1";
    const HASH: &str = "hash-a";
    const PATH: &str = "src/types.rs";

    fn owner() -> VisibleOwnerKey {
        VisibleOwnerKey {
            path: PATH.to_string(),
            content_hash: HASH.to_string(),
            machine_id: MACHINE.to_string(),
        }
    }

    fn visible_all() -> VisibleFileMap {
        VisibleFileMap {
            owners: HashSet::from([owner()]),
            overlay_shadowed_paths: HashSet::new(),
        }
    }

    fn endpoint(kind: CandidateEndpointKind, id: &str) -> CandidateEndpoint {
        CandidateEndpoint {
            kind,
            id: id.to_string(),
            name: Some(id.to_string()),
            file: Some(PATH.to_string()),
            content_hash: Some(HASH.to_string()),
            machine_id: Some(MACHINE.to_string()),
        }
    }

    fn symbol(id: &str) -> CandidateEndpoint {
        endpoint(CandidateEndpointKind::Symbol, id)
    }

    fn external(id: &str) -> CandidateEndpoint {
        let mut node = endpoint(CandidateEndpointKind::External, id);
        node.file = None;
        node.content_hash = None;
        node.machine_id = None;
        node
    }

    fn unresolved(id: &str) -> CandidateEndpoint {
        let mut node = endpoint(CandidateEndpointKind::Unresolved, id);
        node.file = None;
        node.content_hash = None;
        node.machine_id = None;
        node
    }

    fn row(
        source: CandidateEndpoint,
        target: CandidateEndpoint,
        rel: &str,
        edge_id: i64,
    ) -> HeritageHopRow {
        HeritageHopRow {
            source,
            target,
            rel: rel.to_string(),
            edge_id,
            owner_path: PATH.to_string(),
            owner_hash: HASH.to_string(),
            owner_machine: MACHINE.to_string(),
            overlay_shadowed: false,
        }
    }

    fn inherit(source: &str, target: &str, edge_id: i64) -> HeritageHopRow {
        row(symbol(source), symbol(target), "INHERITS", edge_id)
    }

    fn catalog_fetch(
        catalog: &[HeritageHopRow],
        frontier: &[CandidateEndpoint],
        direction: HeritageDirection,
        after: Option<&HeritageCursor>,
        page_size: usize,
    ) -> Vec<HeritageHopRow> {
        let ids = frontier
            .iter()
            .map(|endpoint| endpoint.id.as_str())
            .collect::<HashSet<_>>();
        let mut matched = catalog
            .iter()
            .filter(|edge| match direction {
                HeritageDirection::Ancestors => ids.contains(edge.source.id.as_str()),
                HeritageDirection::Descendants => ids.contains(edge.target.id.as_str()),
            })
            .cloned()
            .collect::<Vec<_>>();
        matched.sort_by(|left, right| left.order_key().cmp(&right.order_key()));
        matched
            .into_iter()
            .filter(|edge| after.is_none_or(|cursor| edge.is_after(cursor)))
            .take(page_size)
            .collect()
    }

    fn walk(
        seed: &str,
        depth: u32,
        visible: VisibleFileMap,
        catalog: Vec<HeritageHopRow>,
    ) -> ChgWalk {
        walk_chg(
            symbol(seed),
            depth,
            |_| Ok(visible.clone()),
            |frontier, direction| {
                exhaust_heritage_hop(
                    |cursor| {
                        Ok(catalog_fetch(
                            &catalog,
                            frontier,
                            direction,
                            cursor,
                            MAX_DECLARED_EDGE_LIMIT,
                        ))
                    },
                    MAX_DECLARED_EDGE_LIMIT,
                )
            },
        )
        .expect("class-hierarchy walk")
    }

    fn node_ids(walk: &ChgWalk) -> Vec<String> {
        let mut ids = walk
            .nodes
            .iter()
            .map(|node| node.key.canonical())
            .collect::<Vec<_>>();
        ids.sort();
        ids
    }

    fn edge_triples(walk: &ChgWalk) -> Vec<(String, String, String)> {
        let mut triples = walk
            .edges
            .iter()
            .map(|edge| {
                (
                    edge.source.canonical(),
                    edge.target.canonical(),
                    edge.rel.clone(),
                )
            })
            .collect::<Vec<_>>();
        triples.sort();
        triples
    }

    fn payload_for(seed: &str, walked: &ChgWalk) -> serde_json::Value {
        let payload = build_view_payload(
            "proj-a",
            "/abs",
            GraphViewKind::ClassHierarchy,
            crate::commands::graph::view::render::ViewSeed {
                id: seed.into(),
                name: seed.into(),
                kind: "symbol".into(),
                file: None,
            },
            8,
            walked.incoming_truncated,
            walked.outgoing_truncated,
            None,
            walked.nodes.clone(),
            walked.edges.clone(),
            Vec::new(),
        )
        .expect("payload");
        serde_json::to_value(&payload).expect("json")
    }

    #[test]
    fn class_hierarchy_includes_base_and_derived_both_directions() {
        let catalog = vec![inherit("Derived", "Base", 1)];
        let from_derived = walk("Derived", 8, visible_all(), catalog.clone());
        let from_base = walk("Base", 8, visible_all(), catalog);
        assert_eq!(
            edge_triples(&from_derived),
            vec![(
                "symbol:Derived".into(),
                "symbol:Base".into(),
                "INHERITS".into()
            )]
        );
        assert_eq!(edge_triples(&from_derived), edge_triples(&from_base));
        assert_eq!(
            node_ids(&from_derived),
            vec!["symbol:Base".to_string(), "symbol:Derived".to_string()]
        );
    }

    #[test]
    fn class_hierarchy_depth_caps_chain() {
        let catalog = vec![inherit("C", "B", 1), inherit("B", "A", 2)];
        let depth_one = walk("C", 1, visible_all(), catalog.clone());
        let depth_default = walk("C", 8, visible_all(), catalog);
        assert_eq!(
            node_ids(&depth_one),
            vec!["symbol:B".to_string(), "symbol:C".to_string()]
        );
        assert!(!node_ids(&depth_one).contains(&"symbol:A".to_string()));
        assert_eq!(
            node_ids(&depth_default),
            vec![
                "symbol:A".to_string(),
                "symbol:B".to_string(),
                "symbol:C".to_string()
            ]
        );
    }

    #[test]
    fn class_hierarchy_diamond_is_complete_dag() {
        let catalog = vec![
            inherit("D", "B", 1),
            inherit("D", "C", 2),
            inherit("B", "A", 3),
            inherit("C", "A", 4),
        ];
        let depth_one = walk("D", 1, visible_all(), catalog.clone());
        let depth_default = walk("D", 8, visible_all(), catalog.clone());
        let from_a = walk("A", 8, visible_all(), catalog);
        assert!(!node_ids(&depth_one).contains(&"symbol:A".to_string()));
        assert!(depth_one.outgoing_truncated);
        assert!(!depth_one.incoming_truncated);
        assert_eq!(
            node_ids(&depth_default),
            vec![
                "symbol:A".to_string(),
                "symbol:B".to_string(),
                "symbol:C".to_string(),
                "symbol:D".to_string()
            ]
        );
        assert!(!depth_default.outgoing_truncated);
        assert_eq!(edge_triples(&depth_default).len(), 4);
        assert_eq!(node_ids(&from_a), node_ids(&depth_default));
        assert_eq!(edge_triples(&from_a), edge_triples(&depth_default));
    }

    #[test]
    fn class_hierarchy_preserves_heritage_subtypes() {
        let catalog = vec![
            row(symbol("Derived"), symbol("Base"), "INHERITS", 1),
            row(symbol("Derived"), symbol("Trait"), "IMPLEMENTS", 2),
            row(symbol("Derived"), symbol("Super"), "EXTENDS", 3),
        ];
        let walked = walk("Derived", 1, visible_all(), catalog);
        let rels = walked
            .edges
            .iter()
            .map(|edge| edge.rel.as_str())
            .collect::<HashSet<_>>();
        assert_eq!(rels, HashSet::from(["INHERITS", "EXTENDS", "IMPLEMENTS"]));
        let json = payload_for("Derived", &walked);
        let mermaid = json["mermaid"].as_str().expect("mermaid");
        assert!(mermaid.contains("INHERITS"));
        assert!(mermaid.contains("EXTENDS"));
        assert!(mermaid.contains("IMPLEMENTS"));
        let public_rels = json["edges"]
            .as_array()
            .expect("edges")
            .iter()
            .map(|edge| edge["rel"].as_str().unwrap().to_string())
            .collect::<HashSet<_>>();
        assert_eq!(
            public_rels,
            HashSet::from([
                "INHERITS".to_string(),
                "EXTENDS".to_string(),
                "IMPLEMENTS".to_string()
            ])
        );
    }

    #[test]
    fn class_hierarchy_includes_external_and_unresolved_terminals() {
        let catalog = vec![
            row(symbol("Derived"), external("ext-1"), "INHERITS", 1),
            row(symbol("Derived"), unresolved("miss-1"), "EXTENDS", 2),
        ];
        let walked = walk("Derived", 8, visible_all(), catalog);
        let ids = node_ids(&walked);
        assert!(ids.contains(&"symbol:Derived".into()));
        assert!(ids.contains(&"external:ext-1".into()));
        assert!(ids.contains(&"unresolved:miss-1".into()));
        assert_eq!(walked.nodes.len(), 3);
        assert!(!walked.outgoing_truncated);
        let json = payload_for("Derived", &walked);
        let mermaid = json["mermaid"].as_str().expect("mermaid");
        assert!(mermaid.contains("INHERITS"));
        assert!(mermaid.contains("EXTENDS"));
    }

    #[test]
    fn class_hierarchy_paginates_hop_to_exhaustion() {
        let mut catalog = Vec::with_capacity(MAX_DECLARED_EDGE_LIMIT + 1);
        for index in 0..=MAX_DECLARED_EDGE_LIMIT {
            catalog.push(inherit("Seed", &format!("N{index:05}"), index as i64));
        }
        let collected = exhaust_heritage_hop(
            |cursor| {
                Ok(catalog_fetch(
                    &catalog,
                    &[symbol("Seed")],
                    HeritageDirection::Ancestors,
                    cursor,
                    MAX_DECLARED_EDGE_LIMIT,
                ))
            },
            MAX_DECLARED_EDGE_LIMIT,
        )
        .expect("exhaust hop");
        assert_eq!(collected.len(), MAX_DECLARED_EDGE_LIMIT + 1);
        let walked = walk("Seed", 1, visible_all(), catalog);
        assert_eq!(walked.edges.len(), MAX_DECLARED_EDGE_LIMIT + 1);
    }

    #[test]
    fn class_hierarchy_pagination_is_total_order() {
        let catalog = vec![
            row(symbol("A"), symbol("B"), "EXTENDS", 2),
            row(symbol("A"), symbol("C"), "INHERITS", 1),
            row(symbol("A"), symbol("B"), "IMPLEMENTS", 1),
            row(symbol("A"), symbol("B"), "EXTENDS", 1),
        ];
        let collected = exhaust_heritage_hop(
            |cursor| {
                Ok(catalog_fetch(
                    &catalog,
                    &[symbol("A")],
                    HeritageDirection::Ancestors,
                    cursor,
                    2,
                ))
            },
            2,
        )
        .expect("exhaust hop");
        let keys = collected
            .iter()
            .map(|edge| {
                (
                    edge.source.id.clone(),
                    edge.target.id.clone(),
                    edge.rel.clone(),
                    edge.edge_id,
                )
            })
            .collect::<Vec<_>>();
        assert_eq!(
            keys,
            vec![
                ("A".into(), "B".into(), "EXTENDS".into(), 1),
                ("A".into(), "B".into(), "EXTENDS".into(), 2),
                ("A".into(), "B".into(), "IMPLEMENTS".into(), 1),
                ("A".into(), "C".into(), "INHERITS".into(), 1),
            ]
        );
        let mut unique = HashSet::new();
        for key in &keys {
            assert!(unique.insert(key.clone()));
        }
    }

    #[test]
    fn class_hierarchy_excludes_siblings() {
        let catalog = vec![inherit("Left", "Base", 1), inherit("Right", "Base", 2)];
        let walked = walk("Left", 8, visible_all(), catalog);
        let ids = node_ids(&walked);
        assert!(ids.contains(&"symbol:Left".into()));
        assert!(ids.contains(&"symbol:Base".into()));
        assert!(!ids.contains(&"symbol:Right".into()));
        assert_eq!(walked.edges.len(), 1);
    }

    #[test]
    fn class_hierarchy_includes_external_and_unresolved_sources() {
        let catalog = vec![
            row(external("ext-impl"), symbol("LocalTrait"), "IMPLEMENTS", 1),
            row(
                unresolved("miss-impl"),
                symbol("LocalTrait"),
                "IMPLEMENTS",
                2,
            ),
        ];
        let walked = walk("LocalTrait", 8, visible_all(), catalog);
        let ids = node_ids(&walked);
        assert!(ids.contains(&"symbol:LocalTrait".into()));
        assert!(ids.contains(&"external:ext-impl".into()));
        assert!(ids.contains(&"unresolved:miss-impl".into()));
        assert_eq!(
            edge_triples(&walked),
            vec![
                (
                    "external:ext-impl".into(),
                    "symbol:LocalTrait".into(),
                    "IMPLEMENTS".into()
                ),
                (
                    "unresolved:miss-impl".into(),
                    "symbol:LocalTrait".into(),
                    "IMPLEMENTS".into()
                ),
            ]
        );
        assert!(!walked.incoming_truncated);
    }

    #[test]
    fn class_hierarchy_applies_visible_owner_set_before_hop_cut() {
        let mut catalog = Vec::with_capacity(MAX_DECLARED_EDGE_LIMIT + 2);
        for index in 0..MAX_DECLARED_EDGE_LIMIT {
            let mut edge = inherit("C", &format!("Inv{index:05}"), index as i64);
            edge.owner_hash = "hash-old".to_string();
            catalog.push(edge);
        }
        catalog.push(inherit("C", "B", 10_000));
        let mut hidden_grandparent = inherit("B", "A", 10_001);
        hidden_grandparent.owner_hash = "hash-old".to_string();
        catalog.push(hidden_grandparent);
        let walked = walk("C", 1, visible_all(), catalog);
        assert_eq!(
            node_ids(&walked),
            vec!["symbol:B".to_string(), "symbol:C".to_string()]
        );
        assert!(!node_ids(&walked).contains(&"symbol:A".to_string()));
        assert!(!walked.outgoing_truncated);
        assert_eq!(walked.edges.len(), 1);
    }

    #[test]
    fn class_hierarchy_descendant_depth_caps_incoming() {
        let catalog = vec![inherit("C", "B", 1), inherit("B", "A", 2)];
        let depth_one = walk("A", 1, visible_all(), catalog.clone());
        let depth_full = walk("A", 8, visible_all(), catalog);
        assert_eq!(
            node_ids(&depth_one),
            vec!["symbol:A".to_string(), "symbol:B".to_string()]
        );
        assert!(!node_ids(&depth_one).contains(&"symbol:C".to_string()));
        assert!(depth_one.incoming_truncated);
        assert!(!depth_one.outgoing_truncated);
        assert_eq!(
            node_ids(&depth_full),
            vec![
                "symbol:A".to_string(),
                "symbol:B".to_string(),
                "symbol:C".to_string()
            ]
        );
        assert!(!depth_full.incoming_truncated);
    }

    #[test]
    fn class_hierarchy_pagination_uses_internal_edge_id() {
        let catalog = vec![
            inherit("Derived", "Base", 11),
            inherit("Derived", "Base", 7),
        ];
        let collected = exhaust_heritage_hop(
            |cursor| {
                Ok(catalog_fetch(
                    &catalog,
                    &[symbol("Derived")],
                    HeritageDirection::Ancestors,
                    cursor,
                    1,
                ))
            },
            1,
        )
        .expect("exhaust hop");
        assert_eq!(
            collected
                .iter()
                .map(|edge| edge.edge_id)
                .collect::<Vec<_>>(),
            vec![7, 11]
        );
        let walked = walk("Derived", 1, visible_all(), catalog);
        assert_eq!(walked.edges.len(), 1);
        let json = payload_for("Derived", &walked);
        let edge = &json["edges"][0];
        assert!(edge.get("edge_id").is_none());
        assert_eq!(edge["rel"], "INHERITS");
        assert_eq!(edge["source"], "symbol:Derived");
        assert_eq!(edge["target"], "symbol:Base");
    }

    #[test]
    fn class_hierarchy_binds_project_on_both_endpoints() {
        let (outgoing, params) = heritage_hop_query(
            "proj-a",
            HeritageDirection::Ancestors,
            &["sym-1".to_string()],
            None,
            MAX_DECLARED_EDGE_LIMIT,
        );
        assert!(outgoing.contains("(source {project: $project})"));
        assert!(outgoing.contains("(target {project: $project})"));
        assert!(
            outgoing
                .contains("source:CodeSymbol OR source:ExternalSymbol OR source:UnresolvedCallee")
        );
        assert!(
            outgoing
                .contains("target:CodeSymbol OR target:ExternalSymbol OR target:UnresolvedCallee")
        );
        assert!(outgoing.contains("[r:INHERITS|EXTENDS|IMPLEMENTS]"));
        assert!(outgoing.contains("id(r) AS edge_id"));
        assert!(outgoing.contains("type(r) AS rel"));
        assert!(outgoing.contains("ORDER BY source, target, rel, edge_id"));
        assert!(outgoing.contains(&format!("LIMIT {MAX_DECLARED_EDGE_LIMIT}")));
        assert!(!outgoing.contains(&format!("LIMIT {}", MAX_DECLARED_EDGE_LIMIT + 1)));
        assert!(outgoing.contains("source.id IN"));
        assert_eq!(params.get("project").map(String::as_str), Some("'proj-a'"));
        assert!(!outgoing.contains("proj-b"));

        let (incoming, _) = heritage_hop_query(
            "proj-a",
            HeritageDirection::Descendants,
            &["sym-1".to_string()],
            None,
            MAX_DECLARED_EDGE_LIMIT,
        );
        assert!(incoming.contains("(source {project: $project})"));
        assert!(incoming.contains("(target {project: $project})"));
        assert!(incoming.contains("target.id IN"));

        let cursor = HeritageCursor {
            source: "s".into(),
            target: "t".into(),
            rel: "EXTENDS".into(),
            edge_id: 9,
        };
        let (paged, paged_params) = heritage_hop_query(
            "proj-a",
            HeritageDirection::Ancestors,
            &["sym-1".to_string()],
            Some(&cursor),
            MAX_DECLARED_EDGE_LIMIT,
        );
        assert!(paged.contains("type(r)"));
        assert!(paged.contains("id(r)"));
        assert_eq!(
            paged_params.get("after_edge").map(String::as_str),
            Some("9")
        );
    }
}
