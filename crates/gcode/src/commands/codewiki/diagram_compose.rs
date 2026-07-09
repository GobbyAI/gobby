//! LLM-composed Mermaid flowcharts under the evidence contract (#17521).
//!
//! The LLM composes architectural diagrams; deterministic code grounds and
//! verifies — the same contract as prose. A caller supplies a
//! [`DiagramEvidence`] graph (real dependency / call / import / documented-flow
//! edges from the code index and the workspace [`super::SystemModel`]), the
//! model picks the story, and this module enforces:
//!
//! * **Edge verification** — every drawn arrow must match a supplied evidence
//!   edge (the diagram analog of citation grounding). Unevidenced arrows are
//!   rejected; a repair pass re-prompts the model with the exact violations,
//!   and whatever still fails verification is dropped deterministically.
//! * **Broad-compat syntax subset** — only `flowchart TD|LR` headers, plain
//!   node declarations, and `-->` / `-.->` arrows survive normalization; the
//!   emitted body is re-rendered from verified content with evidence labels
//!   and Mermaid-native escaping, so model formatting quirks cannot leak.
//! * **No disconnected islands** — nodes that no surviving arrow touches are
//!   dropped, and when the verified graph splits into multiple weakly-connected
//!   components only the largest is kept.
//! * **Valid-Mermaid gate** — the final fenced block must pass
//!   [`is_valid_mermaid`] (the shared gobby-core gate), or nothing is emitted.
//!
//! A page whose evidence has no edges gets no diagram: composing one would
//! fabricate a flow no source evidences, exactly like an uncited prose claim.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write as _;

use gobby_core::vault::mermaid::{escape_label as mermaid_label, is_valid_mermaid};

use super::types::TextGenerator;
use super::{GenerationContent, ToolLoopGenerator, generate_aggregate, prompts};

/// Visual shape for an evidence node, applied deterministically at
/// normalization so the model cannot unbalance a bracket pair.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub(crate) enum NodeShape {
    /// `id["label"]` — the default component box.
    #[default]
    Box,
    /// `id(["label"])` — runnable entry points (binaries).
    Stadium,
    /// `id[("label")]` — service/datastore boundaries.
    Cylinder,
}

/// One node the model is allowed to draw.
#[derive(Clone, Debug)]
pub(crate) struct EvidenceNode {
    /// Stable Mermaid identifier the model must reference verbatim.
    pub(crate) id: String,
    /// Human label, escaped at normalization (never trusted from the model).
    pub(crate) label: String,
    pub(crate) shape: NodeShape,
}

/// One directed edge the model is allowed to draw. Style and label are
/// canonical: the normalizer re-attaches them from the evidence regardless of
/// how the model wrote the arrow.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) struct EvidenceEdge {
    pub(crate) from: String,
    pub(crate) to: String,
    /// Optional arrow label (e.g. a service dependency strength).
    pub(crate) label: Option<String>,
    /// Dotted (`-.->`) versus solid (`-->`) arrow.
    pub(crate) dotted: bool,
}

/// The full evidence graph supplied to one composition call.
#[derive(Clone, Debug, Default)]
pub(crate) struct DiagramEvidence {
    pub(crate) nodes: Vec<EvidenceNode>,
    pub(crate) edges: Vec<EvidenceEdge>,
}

impl DiagramEvidence {
    /// True when there is nothing evidenced to draw: fewer than two nodes or
    /// no edges. Callers emit no diagram in that case (normal, not degraded).
    pub(crate) fn is_sparse(&self) -> bool {
        self.nodes.len() < 2 || self.edges.is_empty()
    }

    pub(crate) fn push_node(
        &mut self,
        id: impl Into<String>,
        label: impl Into<String>,
        shape: NodeShape,
    ) {
        let id = id.into();
        if self.nodes.iter().all(|node| node.id != id) {
            self.nodes.push(EvidenceNode {
                id,
                label: label.into(),
                shape,
            });
        }
    }

    pub(crate) fn push_edge(
        &mut self,
        from: impl Into<String>,
        to: impl Into<String>,
        label: Option<String>,
        dotted: bool,
    ) {
        let from = from.into();
        let to = to.into();
        debug_assert!(
            self.node(&from).is_some(),
            "diagram evidence edge references missing source node `{from}`"
        );
        debug_assert!(
            self.node(&to).is_some(),
            "diagram evidence edge references missing target node `{to}`"
        );
        let edge = EvidenceEdge {
            from,
            to,
            label,
            dotted,
        };
        if edge.from != edge.to && !self.edges.contains(&edge) {
            self.edges.push(edge);
        }
    }

    fn node(&self, id: &str) -> Option<&EvidenceNode> {
        self.nodes.iter().find(|node| node.id == id)
    }

    fn edge(&self, from: &str, to: &str) -> Option<&EvidenceEdge> {
        self.edges
            .iter()
            .find(|edge| edge.from == from && edge.to == to)
    }

    /// Render the evidence block of the composition prompt: the only nodes and
    /// arrows the model may draw.
    fn prompt_block(&self) -> String {
        let mut block = String::from("Nodes (id: label):\n");
        for node in &self.nodes {
            let _ = writeln!(block, "- {}: {}", node.id, node.label);
        }
        block.push_str("\nEvidence edges (the only arrows you may draw):\n");
        for edge in &self.edges {
            match &edge.label {
                Some(label) => {
                    let _ = writeln!(block, "- {} -> {} ({label})", edge.from, edge.to);
                }
                None => {
                    let _ = writeln!(block, "- {} -> {}", edge.from, edge.to);
                }
            }
        }
        block
    }
}

/// One verification failure, fed back to the model on the repair attempt.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum FlowchartIssue {
    /// The output had no `flowchart TD|LR` (or `graph TD|LR`) header.
    NotAFlowchart,
    /// A line the broad-compat subset cannot parse.
    UnparseableLine(String),
    /// A node id that is not in the supplied evidence.
    UnknownNode(String),
    /// An arrow that matches no supplied evidence edge.
    UnevidencedArrow(String, String),
    /// Nodes dropped because they were disconnected from the main component.
    DisconnectedIsland(Vec<String>),
}

impl std::fmt::Display for FlowchartIssue {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NotAFlowchart => {
                write!(
                    f,
                    "output did not start with 'flowchart TD' or 'flowchart LR'"
                )
            }
            Self::UnparseableLine(line) => write!(f, "unparseable line: {line}"),
            Self::UnknownNode(id) => write!(f, "node '{id}' is not in the supplied evidence"),
            Self::UnevidencedArrow(from, to) => {
                write!(
                    f,
                    "arrow '{from} --> {to}' matches no supplied evidence edge"
                )
            }
            Self::DisconnectedIsland(ids) => {
                write!(f, "disconnected island dropped: {}", ids.join(", "))
            }
        }
    }
}

/// Flow direction the model chose; preserved through normalization.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
enum FlowDirection {
    #[default]
    TopDown,
    LeftRight,
}

impl FlowDirection {
    fn header(self) -> &'static str {
        match self {
            Self::TopDown => "flowchart TD",
            Self::LeftRight => "flowchart LR",
        }
    }
}

/// The model's drawing reduced to verified content: direction plus the
/// evidence edges it drew, in drawn order.
struct VerifiedFlowchart {
    direction: FlowDirection,
    edges: Vec<EvidenceEdge>,
}

/// Verify one model drawing against the evidence. Returns the surviving
/// verified content plus every issue found (the repair-prompt feedback).
/// Surviving content is already island-free.
fn verify_candidate(
    candidate: &str,
    evidence: &DiagramEvidence,
) -> (Option<VerifiedFlowchart>, Vec<FlowchartIssue>) {
    let mut issues = Vec::new();
    let mut direction = None;
    let mut edges: Vec<EvidenceEdge> = Vec::new();

    for raw_line in strip_optional_fence(candidate).lines() {
        let line = raw_line.trim();
        if line.is_empty() || line.starts_with("%%") {
            continue;
        }
        if direction.is_none() {
            match parse_header(line) {
                Some(parsed) => direction = Some(parsed),
                None => {
                    issues.push(FlowchartIssue::NotAFlowchart);
                    return (None, issues);
                }
            }
            continue;
        }
        // Structural lines the subset tolerates but never re-emits: grouping
        // and styling add renderer-compat risk without adding evidence.
        if is_ignorable_structure(line) {
            continue;
        }
        if let Some((from, to)) = parse_arrow(line) {
            for id in [&from, &to] {
                if evidence.node(id).is_none() {
                    issues.push(FlowchartIssue::UnknownNode(id.clone()));
                }
            }
            match evidence.edge(&from, &to) {
                Some(edge) if evidence.node(&from).is_some() && evidence.node(&to).is_some() => {
                    if !edges.contains(edge) {
                        edges.push(edge.clone());
                    }
                }
                Some(_) => {}
                None => issues.push(FlowchartIssue::UnevidencedArrow(from, to)),
            }
            continue;
        }
        if let Some(id) = parse_node_declaration(line) {
            // Node declarations only matter for grounding: an unknown id is a
            // violation; a known id is redundant (the normalizer re-declares
            // every drawn node from evidence).
            if evidence.node(&id).is_none() {
                issues.push(FlowchartIssue::UnknownNode(id));
            }
            continue;
        }
        issues.push(FlowchartIssue::UnparseableLine(line.to_string()));
    }

    let Some(direction) = direction else {
        issues.push(FlowchartIssue::NotAFlowchart);
        return (None, issues);
    };

    let (edges, island_issue) = keep_largest_component(edges);
    if let Some(issue) = island_issue {
        issues.push(issue);
    }
    if edges.is_empty() {
        return (None, issues);
    }
    (Some(VerifiedFlowchart { direction, edges }), issues)
}

/// Drop every edge outside the largest weakly-connected component so the
/// emitted flowchart never contains disconnected islands. Ties break toward
/// the component drawn first.
fn keep_largest_component(edges: Vec<EvidenceEdge>) -> (Vec<EvidenceEdge>, Option<FlowchartIssue>) {
    fn root<'a>(parent: &BTreeMap<&'a str, &'a str>, mut id: &'a str) -> &'a str {
        while parent[id] != id {
            id = parent[id];
        }
        id
    }

    if edges.is_empty() {
        return (edges, None);
    }
    // Union-find over node ids, in drawn order.
    let mut parent: BTreeMap<&str, &str> = BTreeMap::new();
    for edge in &edges {
        parent.entry(&edge.from).or_insert(&edge.from);
        parent.entry(&edge.to).or_insert(&edge.to);
        let from_root = root(&parent, &edge.from);
        let to_root = root(&parent, &edge.to);
        if from_root != to_root {
            parent.insert(from_root, to_root);
        }
    }
    let mut component_sizes: BTreeMap<&str, usize> = BTreeMap::new();
    for id in parent.keys().copied().collect::<Vec<_>>() {
        *component_sizes.entry(root(&parent, id)).or_default() += 1;
    }
    if component_sizes.len() <= 1 {
        return (edges, None);
    }
    let max_size = component_sizes.values().copied().max().unwrap_or(0);
    // First drawn edge whose component has the max size anchors the winner.
    let winner = edges
        .iter()
        .map(|edge| root(&parent, &edge.from))
        .find(|component_root| component_sizes[component_root] == max_size);
    let Some(winner) = winner else {
        return (edges, None);
    };
    let winner = winner.to_string();
    let mut dropped: BTreeSet<String> = BTreeSet::new();
    let kept = edges
        .iter()
        .filter(|edge| {
            let in_winner = root(&parent, &edge.from) == winner.as_str();
            if !in_winner {
                dropped.insert(edge.from.clone());
                dropped.insert(edge.to.clone());
            }
            in_winner
        })
        .cloned()
        .collect();
    let issue = (!dropped.is_empty())
        .then(|| FlowchartIssue::DisconnectedIsland(dropped.into_iter().collect()));
    (kept, issue)
}

/// Strip one optional ```` ```mermaid ```` (or bare ```` ``` ````) fence the
/// model may have wrapped its answer in despite instructions.
fn strip_optional_fence(text: &str) -> &str {
    let trimmed = text.trim();
    let Some(rest) = trimmed.strip_prefix("```") else {
        return trimmed;
    };
    let rest = rest.strip_prefix("mermaid").unwrap_or(rest);
    rest.trim_start_matches(['\r', '\n'])
        .strip_suffix("```")
        .map(str::trim_end)
        .unwrap_or(trimmed)
}

fn parse_header(line: &str) -> Option<FlowDirection> {
    let mut tokens = line.split_whitespace();
    let keyword = tokens.next()?;
    if keyword != "flowchart" && keyword != "graph" {
        return None;
    }
    match tokens.next() {
        Some("TD" | "TB") => Some(FlowDirection::TopDown),
        Some("LR") => Some(FlowDirection::LeftRight),
        _ => None,
    }
}

/// Grouping/styling lines the parser tolerates and drops.
fn is_ignorable_structure(line: &str) -> bool {
    line == "end"
        || line.starts_with("subgraph ")
        || line.starts_with("classDef ")
        || line.starts_with("class ")
        || line.starts_with("style ")
        || line.starts_with("linkStyle ")
        || line.starts_with("direction ")
}

/// Parse one arrow line into `(from, to)` node ids. Accepts solid (`-->`,
/// `==>`) and dotted (`-.->`) arrows with an optional `|label|` segment, and
/// endpoints that carry inline node declarations (`a["x"] --> b`). Chains
/// (`a --> b --> c`) and multi-endpoints (`a & b --> c`) are outside the
/// subset and fail to parse.
fn parse_arrow(line: &str) -> Option<(String, String)> {
    let (from_part, rest) = split_on_arrow(line)?;
    // An optional |label| immediately after the arrow head.
    let rest = rest.trim_start();
    let to_part = if let Some(after) = rest.strip_prefix('|') {
        after.split_once('|')?.1
    } else {
        rest
    };
    if split_on_arrow(to_part).is_some() {
        // A chained arrow is outside the subset.
        return None;
    }
    let from = endpoint_id(from_part)?;
    let to = endpoint_id(to_part)?;
    (from != to).then_some((from, to))
}

/// Split at the first arrow token, returning the text before it and after it.
fn split_on_arrow(line: &str) -> Option<(&str, &str)> {
    let candidates = ["-.->", "-->", "==>"];
    let (index, token) = candidates
        .iter()
        .filter_map(|token| line.find(token).map(|index| (index, *token)))
        .min_by_key(|(index, _)| *index)?;
    Some((&line[..index], &line[index + token.len()..]))
}

/// Reduce an arrow endpoint (possibly carrying an inline declaration such as
/// `a["label"]`) to its bare node id.
fn endpoint_id(part: &str) -> Option<String> {
    let part = part.trim();
    if part.contains('&') {
        // `a & b --> c` multi-endpoints are outside the subset.
        return None;
    }
    let id: String = part
        .chars()
        .take_while(|c| c.is_ascii_alphanumeric() || *c == '_')
        .collect();
    if id.is_empty() {
        return None;
    }
    // Whatever follows the id must be a (single) shape declaration, not more
    // syntax we would silently misread.
    let rest = part[id.len()..].trim();
    if rest.is_empty() || parse_shape_suffix(rest) {
        Some(id)
    } else {
        None
    }
}

/// Parse a standalone node-declaration line, returning its id.
fn parse_node_declaration(line: &str) -> Option<String> {
    let id: String = line
        .chars()
        .take_while(|c| c.is_ascii_alphanumeric() || *c == '_')
        .collect();
    if id.is_empty() {
        return None;
    }
    let rest = line[id.len()..].trim();
    if rest.is_empty() {
        // A bare id on its own line is a (redundant) declaration.
        return Some(id);
    }
    parse_shape_suffix(rest).then_some(id)
}

/// True when `rest` is one complete bracketed shape body such as `["x"]`,
/// `(["x"])`, `[("x")]`, `{"x"}`, or `{{"x"}}`.
fn parse_shape_suffix(rest: &str) -> bool {
    const SHAPES: [(&str, &str); 7] = [
        ("([", "])"),
        ("[(", ")]"),
        ("{{", "}}"),
        ("[", "]"),
        ("(", ")"),
        ("{", "}"),
        (">", "]"),
    ];
    SHAPES.iter().any(|(open, close)| {
        rest.strip_prefix(open)
            .and_then(|inner| inner.strip_suffix(close))
            .is_some_and(|inner| !inner.contains(['[', ']', '{', '}']) || inner.starts_with('"'))
    })
}

/// Re-render the verified content deterministically: evidence labels with
/// Mermaid-native escaping, canonical arrow styles, nodes declared in first
/// drawn appearance order. The model chose the story (which evidenced edges,
/// what direction, what order); the normalizer owns the syntax.
fn normalize(verified: &VerifiedFlowchart, evidence: &DiagramEvidence) -> Option<String> {
    let mut node_order: Vec<&str> = Vec::new();
    for edge in &verified.edges {
        for id in [edge.from.as_str(), edge.to.as_str()] {
            if !node_order.contains(&id) {
                node_order.push(id);
            }
        }
    }
    if node_order.len() < 2 {
        return None;
    }

    let mut body = String::from(verified.direction.header());
    body.push('\n');
    for id in &node_order {
        let node = evidence.node(id)?;
        let label = mermaid_label(&node.label);
        let declaration = match node.shape {
            NodeShape::Box => format!("    {id}[\"{label}\"]"),
            NodeShape::Stadium => format!("    {id}([\"{label}\"])"),
            NodeShape::Cylinder => format!("    {id}[(\"{label}\")]"),
        };
        body.push_str(&declaration);
        body.push('\n');
    }
    for edge in &verified.edges {
        let arrow = if edge.dotted { "-.->" } else { "-->" };
        let line = match &edge.label {
            Some(label) => format!(
                "    {} {arrow}|\"{}\"| {}",
                edge.from,
                mermaid_label(label),
                edge.to
            ),
            None => format!("    {} {arrow} {}", edge.from, edge.to),
        };
        body.push_str(&line);
        body.push('\n');
    }

    let block = fence(&body);
    is_valid_mermaid(&block).then_some(block)
}

/// Wrap a diagram body in a ```` ```mermaid ```` fence with a trailing newline.
pub(crate) fn fence(body: &str) -> String {
    let trimmed = body.trim_end_matches('\n');
    format!("```mermaid\n{trimmed}\n```\n")
}

/// LLM attempts per diagram: one composition plus one repair re-prompt.
const COMPOSE_ATTEMPTS: usize = 2;

/// Compose one evidence-grounded flowchart. Returns the validated fenced
/// block, or `None` when the evidence is too sparse, no generator is
/// available (AI off), or nothing verifiable survived the repair loop — all
/// normal no-diagram outcomes, never degradation.
pub(crate) fn compose_flowchart(
    generate: &mut Option<&mut TextGenerator<'_>>,
    evidence: &DiagramEvidence,
    context: &str,
) -> Option<String> {
    if evidence.is_sparse() || generate.is_none() {
        return None;
    }

    let base_prompt = format!(
        "Compose one Mermaid flowchart for: {context}.\n\n{}",
        evidence.prompt_block()
    );
    let mut feedback: Option<String> = None;
    let mut best: Option<VerifiedFlowchart> = None;

    for _ in 0..COMPOSE_ATTEMPTS {
        let prompt = match &feedback {
            Some(feedback) => format!(
                "{base_prompt}\nYour previous attempt failed verification:\n{feedback}\n\
                 Redraw the flowchart using only the supplied node ids and evidence edges."
            ),
            None => base_prompt.clone(),
        };
        // Lane A one-shot on the aggregate tier: the evidence is fully
        // supplied in the prompt, so the Lane B tool loop adds cost without
        // adding grounding (same rationale as curated page bodies).
        let mut no_tool_loop: Option<&mut ToolLoopGenerator<'_>> = None;
        let Ok(aggregate) = generate_aggregate(
            &mut no_tool_loop,
            generate,
            &prompt,
            prompts::FLOW_DIAGRAM_SYSTEM,
            context,
        ) else {
            break;
        };
        let candidate = match aggregate.content {
            GenerationContent::Generated(text) => text,
            // A diagram is optional page furniture: a failed or skipped
            // generation means no diagram, never a degraded page.
            GenerationContent::Failed(_) | GenerationContent::Skipped => break,
        };

        let (verified, issues) = verify_candidate(&candidate, evidence);
        if let Some(verified) = verified {
            if issues.is_empty() {
                return normalize(&verified, evidence);
            }
            // Verification dropped something: keep the survivors as the
            // deterministic-repair backstop, but give the model one chance to
            // redraw cleanly.
            if best
                .as_ref()
                .is_none_or(|best| verified.edges.len() > best.edges.len())
            {
                best = Some(verified);
            }
        }
        feedback = Some(
            issues
                .iter()
                .map(|issue| format!("- {issue}"))
                .collect::<Vec<_>>()
                .join("\n"),
        );
    }

    // Deterministic repair: emit what survived edge verification, or nothing.
    best.and_then(|verified| normalize(&verified, evidence))
}

#[cfg(test)]
mod tests {
    use super::super::types::PromptTier;
    use super::*;

    fn evidence() -> DiagramEvidence {
        let mut evidence = DiagramEvidence::default();
        evidence.push_node("a", "Alpha", NodeShape::Box);
        evidence.push_node("b", "Beta (core)", NodeShape::Stadium);
        evidence.push_node("c", "Gamma", NodeShape::Cylinder);
        evidence.push_edge("a", "b", None, false);
        evidence.push_edge("b", "c", Some("required".to_string()), true);
        evidence
    }

    fn compose_with(responses: Vec<String>, evidence: &DiagramEvidence) -> Option<String> {
        let mut responses = responses;
        let mut generator = move |_prompt: &str, system: &str, _tier: PromptTier| {
            assert_eq!(system, prompts::FLOW_DIAGRAM_SYSTEM);
            (!responses.is_empty()).then(|| responses.remove(0))
        };
        let mut generate: Option<&mut TextGenerator<'_>> = Some(&mut generator);
        compose_flowchart(&mut generate, evidence, "test flow")
    }

    #[test]
    fn composes_normalized_block_from_clean_model_output() {
        let block = compose_with(
            vec!["flowchart LR\n    a --> b\n    b -.->|required| c\n".to_string()],
            &evidence(),
        )
        .expect("diagram");
        assert!(block.starts_with("```mermaid\nflowchart LR\n"));
        // Labels come from evidence with Mermaid-native escaping, shapes from
        // the evidence node kinds.
        assert!(block.contains("a[\"Alpha\"]"));
        assert!(block.contains("b([\"Beta #40;core#41;\"])"));
        assert!(block.contains("c[(\"Gamma\")]"));
        // Canonical arrow style and label re-attach from evidence.
        assert!(block.contains("b -.->|\"required\"| c"));
        assert!(is_valid_mermaid(&block));
    }

    #[test]
    fn strips_a_model_added_mermaid_fence() {
        let block = compose_with(
            vec!["```mermaid\nflowchart TD\n    a --> b\n```".to_string()],
            &evidence(),
        )
        .expect("diagram");
        assert!(block.starts_with("```mermaid\nflowchart TD\n"));
        assert_eq!(block.matches("```").count(), 2);
    }

    #[test]
    fn unevidenced_arrow_is_rejected_and_repair_prompt_names_it() {
        // First attempt draws an arrow that matches no evidence edge (reversed
        // direction); the repair prompt must name it and the second, clean
        // attempt wins.
        let mut prompts_seen: Vec<String> = Vec::new();
        let mut responses = vec![
            "flowchart TD\n    a --> b\n    c --> b\n".to_string(),
            "flowchart TD\n    a --> b\n    b -.-> c\n".to_string(),
        ];
        let mut generator = |prompt: &str, _system: &str, _tier: PromptTier| {
            prompts_seen.push(prompt.to_string());
            (!responses.is_empty()).then(|| responses.remove(0))
        };
        let mut generate: Option<&mut TextGenerator<'_>> = Some(&mut generator);
        let block = compose_flowchart(&mut generate, &evidence(), "test flow").expect("diagram");

        assert_eq!(prompts_seen.len(), 2, "one repair re-prompt");
        assert!(
            prompts_seen[1].contains("arrow 'c --> b' matches no supplied evidence edge"),
            "repair prompt must name the unevidenced arrow: {}",
            prompts_seen[1]
        );
        assert!(block.contains("a --> b"));
        assert!(
            !block.contains("c --> b"),
            "unevidenced arrow rejected: {block}"
        );
    }

    #[test]
    fn unevidenced_arrow_is_dropped_when_repair_also_fails() {
        // Both attempts keep the unevidenced arrow: the deterministic repair
        // emits only what survived edge verification.
        let bad = "flowchart TD\n    a --> b\n    c --> a\n".to_string();
        let block = compose_with(vec![bad.clone(), bad], &evidence()).expect("diagram");
        assert!(block.contains("a --> b"));
        assert!(!block.contains("c --> a"));
    }

    #[test]
    fn nothing_survives_means_no_diagram() {
        let bad = "flowchart TD\n    c --> a\n    b --> a\n".to_string();
        assert_eq!(compose_with(vec![bad.clone(), bad], &evidence()), None);
    }

    #[test]
    fn unknown_node_is_rejected() {
        let bad = "flowchart TD\n    a --> b\n    ghost --> b\n".to_string();
        let block = compose_with(vec![bad.clone(), bad], &evidence()).expect("diagram");
        assert!(!block.contains("ghost"));
    }

    #[test]
    fn disconnected_island_is_dropped() {
        let mut evidence = evidence();
        evidence.push_node("x", "Xi", NodeShape::Box);
        evidence.push_node("y", "Ypsilon", NodeShape::Box);
        evidence.push_edge("x", "y", None, false);
        // The model draws two disconnected components; only the larger stays.
        let drawing = "flowchart TD\n    a --> b\n    b -.-> c\n    x --> y\n".to_string();
        let block = compose_with(vec![drawing.clone(), drawing], &evidence).expect("diagram");
        assert!(block.contains("a --> b"));
        assert!(
            !block.contains("x --> y"),
            "island must be dropped: {block}"
        );
        assert!(!block.contains("Xi"));
    }

    #[test]
    fn sparse_evidence_or_missing_generator_yields_none() {
        let mut generate: Option<&mut TextGenerator<'_>> = None;
        assert_eq!(compose_flowchart(&mut generate, &evidence(), "test"), None);

        let mut no_edges = DiagramEvidence::default();
        no_edges.push_node("a", "Alpha", NodeShape::Box);
        no_edges.push_node("b", "Beta", NodeShape::Box);
        assert_eq!(
            compose_with(vec!["flowchart TD\n    a --> b\n".to_string()], &no_edges),
            None
        );
    }

    #[test]
    fn non_flowchart_output_yields_none() {
        let prose = "Here is a diagram description instead of a diagram.".to_string();
        assert_eq!(compose_with(vec![prose.clone(), prose], &evidence()), None);
    }

    #[test]
    fn tolerated_structure_lines_are_dropped_not_fatal() {
        let drawing = "flowchart TD\n    %% comment\n    subgraph g [\"Group\"]\n    a[\"Alpha\"]\n    end\n    a --> b\n    classDef svc fill:#eef;\n".to_string();
        let block = compose_with(vec![drawing], &evidence()).expect("diagram");
        assert!(!block.contains("subgraph"));
        assert!(!block.contains("classDef"));
        assert!(block.contains("a --> b"));
    }

    #[test]
    fn evidence_prompt_block_lists_nodes_and_edges() {
        let block = evidence().prompt_block();
        assert!(block.contains("- a: Alpha"));
        assert!(block.contains("- a -> b"));
        assert!(block.contains("- b -> c (required)"));
    }

    #[test]
    #[should_panic(expected = "diagram evidence edge references missing target node `ghost`")]
    fn evidence_edges_must_reference_existing_nodes() {
        let mut evidence = DiagramEvidence::default();
        evidence.push_node("a", "Alpha", NodeShape::Box);
        evidence.push_edge("a", "ghost", None, false);
    }

    #[test]
    fn failed_repair_attempt_preserves_best_surviving_candidate() {
        let mut responses = vec![Some("flowchart TD\n    a --> b\n    c --> a\n".to_string())];
        let mut generator =
            |_prompt: &str, _system: &str, _tier: PromptTier| responses.pop().flatten();
        let mut generate: Option<&mut TextGenerator<'_>> = Some(&mut generator);

        let block = compose_flowchart(&mut generate, &evidence(), "test flow").expect("diagram");

        assert!(block.contains("a --> b"));
        assert!(!block.contains("c --> a"));
    }

    #[test]
    fn worse_partial_repair_does_not_replace_best_candidate() {
        let mut evidence = evidence();
        evidence.push_node("x", "Xi", NodeShape::Box);
        evidence.push_node("y", "Ypsilon", NodeShape::Box);
        evidence.push_edge("x", "y", None, false);
        let mut responses = vec![
            "flowchart TD\n    a --> b\n    b -.-> c\n    x --> y\n".to_string(),
            "flowchart TD\n    a --> b\n    c --> a\n".to_string(),
        ];
        let mut generator = |_prompt: &str, _system: &str, _tier: PromptTier| {
            (!responses.is_empty()).then(|| responses.remove(0))
        };
        let mut generate: Option<&mut TextGenerator<'_>> = Some(&mut generator);

        let block = compose_flowchart(&mut generate, &evidence, "test flow").expect("diagram");

        assert!(block.contains("a --> b"));
        assert!(
            block.contains("b -.->"),
            "best two-edge survivor should win over one-edge repair: {block}"
        );
        assert!(!block.contains("x --> y"));
        assert!(!block.contains("c --> a"));
    }

    #[test]
    fn parse_arrow_accepts_subset_and_rejects_chains() {
        assert_eq!(
            parse_arrow("a --> b"),
            Some(("a".to_string(), "b".to_string()))
        );
        assert_eq!(
            parse_arrow("a[\"X\"] -.->|\"lbl\"| b([\"Y\"])"),
            Some(("a".to_string(), "b".to_string()))
        );
        assert_eq!(
            parse_arrow("a ==> b"),
            Some(("a".to_string(), "b".to_string()))
        );
        assert_eq!(parse_arrow("a --> b --> c"), None);
        assert_eq!(parse_arrow("a & b --> c"), None);
        assert_eq!(parse_arrow("a --- b"), None);
    }
}
