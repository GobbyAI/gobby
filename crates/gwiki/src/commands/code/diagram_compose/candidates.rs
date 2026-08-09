use super::*;

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
pub(super) struct VerifiedFlowchart {
    direction: FlowDirection,
    pub(super) edges: Vec<EvidenceEdge>,
}

/// Verify one model drawing against the evidence. Returns the surviving
/// verified content plus every issue found (the repair-prompt feedback).
/// Surviving content is already island-free.
pub(super) fn verify_candidate(
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
        while let Some(next) = parent.get(id).copied() {
            if next == id {
                break;
            }
            id = next;
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
pub(super) fn parse_arrow(line: &str) -> Option<(String, String)> {
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
pub(super) fn normalize(
    verified: &VerifiedFlowchart,
    evidence: &DiagramEvidence,
) -> Option<String> {
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
