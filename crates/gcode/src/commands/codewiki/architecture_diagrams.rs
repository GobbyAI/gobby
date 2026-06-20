//! Architectural Mermaid diagrams for the codewiki architecture page.
//!
//! Leaf B of epic #886 (#891). Renders *topology* and *runtime-flow* diagrams
//! from the deterministic [`SystemModel`] built by Leaf A
//! ([`super::system_model::build_system_model`]) — crate↔crate↔service
//! topology plus the standalone-vs-daemon runtime branch, and at least one
//! runtime-flow sequence the model can express.
//!
//! These are *architectural* diagrams seeded strictly from workspace facts on
//! disk, NOT the per-symbol FalkorDB call/import-edge dumps that commit #884
//! deliberately removed. The model is the only source; nothing here reads the
//! code graph or invents components absent from the model.
//!
//! Invariants (the #884 / #878 contract):
//!
//! * **Valid-Mermaid gate.** Every block is checked by [`is_valid_mermaid`]
//!   before it is emitted. A block that fails the gate is OMITTED; a broken or
//!   unparseable ```` ```mermaid ```` fence is never written.
//! * **Non-degrading.** A SystemModel too sparse to draw (no crates/edges from
//!   a fully-partial model) yields no diagram, but that is *normal*, not
//!   degradation. Omitting a diagram never sets `degraded` on the page;
//!   `degraded:model-unavailable` stays reserved for genuine generation
//!   fallback.

use std::collections::BTreeSet;
use std::fmt::Write as _;

use super::system_model::{Edge, RuntimeMode, ServiceKind, SystemModel};

/// Recognised Mermaid diagram headers the validator accepts. `graph` and
/// `flowchart` are the two spellings of the flow diagram; `sequenceDiagram`
/// drives the runtime-flow diagram.
const VALID_HEADERS: [&str; 3] = ["flowchart", "graph", "sequenceDiagram"];

/// Render the architecture diagram section for the page body: a leading prose
/// note, then each well-formed diagram block. Returns `None` when the model is
/// too sparse to draw anything *and* there is no diagram to show — the caller
/// then simply omits the section. A returned string always ends with a
/// trailing blank line and contains only validated fences.
pub(crate) fn render_architecture_diagrams(model: &SystemModel) -> Option<String> {
    let mut blocks: Vec<String> = Vec::new();

    if let Some(topology) = render_topology_flowchart(model) {
        blocks.push(topology);
    }
    if let Some(flow) = render_runtime_flow_sequence(model) {
        blocks.push(flow);
    }

    // Every candidate block passes the gate before it reaches `blocks` (the
    // renderers below validate their own output), but re-gate here as the
    // single choke point so a future renderer cannot leak an invalid fence.
    blocks.retain(|block| is_valid_mermaid(block));
    if blocks.is_empty() {
        return None;
    }

    let mut section = String::new();
    section.push_str("## Architecture Diagrams\n\n");
    section.push_str(
        "These diagrams are derived from the workspace `Cargo.toml` topology — \
member crates, their workspace-internal dependency edges, the service \
boundaries their feature gates pull in, and the standalone-vs-daemon runtime \
branch. They describe structure, not per-symbol call graphs.\n\n",
    );
    section.push_str(
        "Solid edges are workspace-internal crate dependencies. Dotted edges are \
service boundaries, labelled by dependency strength: **required** (the command \
cannot run without it), **degraded-ok** (required product infrastructure with \
degraded command behavior when the service is absent), **optional** (engaged \
only when that routing is selected), and **always** (always-on transport).\n\n",
    );
    for block in blocks {
        section.push_str(&block);
        if !section.ends_with('\n') {
            section.push('\n');
        }
        section.push('\n');
    }
    Some(section)
}

/// Deterministic service matrix for the architecture page: one row per service
/// boundary the model reaches, with a fixed requirement classification and what
/// pulls it in. Seeded only from the [`SystemModel`] — no LLM — so an evaluator
/// gets the at-a-glance "what does this need to run, and what merely degrades"
/// picture. Returns `None` when the model reaches no services (nothing to show).
pub(crate) fn render_service_matrix(model: &SystemModel) -> Option<String> {
    if model.services.is_empty() {
        return None;
    }

    let mut section = String::from("## Services\n\n");
    section.push_str(
        "Derived deterministically from the workspace's Cargo features and service \
boundaries. **Requirement** classifies each service: the PostgreSQL hub is hard-required; \
FalkorDB, Qdrant, and the embedding API are required product infrastructure with degraded \
command behavior when absent (search drops a ranking signal, never the whole result); the \
daemon is optional AI routing; the ghook inbox is always-on transport; and the parsing / \
media toolchains gate AST and multimodal ingest.\n\n",
    );
    section.push_str("| Service | Requirement | Pulled in by |\n");
    section.push_str("| --- | --- | --- |\n");
    for service in &model.services {
        let pulled_in_by = if service.pulled_in_by.is_empty() {
            "workspace".to_string()
        } else {
            service.pulled_in_by.join("; ")
        };
        let _ = writeln!(
            section,
            "| {} | {} | {} |",
            service.name,
            service_requirement(service.kind),
            pulled_in_by
        );
    }
    section.push('\n');
    Some(section)
}

/// Fixed requirement classification for the service matrix — never LLM-drawn.
/// Mirrors the edge labels in [`service_edge_label`] but in full evaluator
/// wording.
fn service_requirement(kind: ServiceKind) -> &'static str {
    match kind {
        ServiceKind::Postgres => "Required (index-backed commands)",
        ServiceKind::Falkor | ServiceKind::Qdrant | ServiceKind::EmbeddingApi => {
            "Required, degraded behavior when absent"
        }
        ServiceKind::Daemon => "Optional (AI routing)",
        ServiceKind::GhookInbox => "Always-on (hook transport)",
        ServiceKind::TreeSitter | ServiceKind::DocumentToolchain | ServiceKind::MediaToolchain => {
            "Toolchain (degraded behavior when absent)"
        }
    }
}

/// Topology flowchart: crates as nodes, dependency edges as arrows, services
/// as a styled boundary subgraph, plus the standalone/daemon runtime branch.
/// Returns `None` when there are no crates to draw (a fully-partial model).
fn render_topology_flowchart(model: &SystemModel) -> Option<String> {
    if model.crates.is_empty() {
        // Nothing to draw — normal for a fully-partial model, not degradation.
        return None;
    }

    let mut body = String::from("flowchart TD\n");

    // Crate nodes, in the model's deterministic (sorted) order.
    body.push_str("    subgraph crates [\"Workspace crates\"]\n");
    for krate in &model.crates {
        let shape = if krate.is_binary && !krate.is_lib {
            // Binaries get a stadium shape to read as runnable entry points.
            ("([\"", "\"])")
        } else {
            ("[\"", "\"]")
        };
        let _ = writeln!(
            body,
            "        {}{}{}{}",
            node_id(&krate.name),
            shape.0,
            mermaid_label(&krate.name),
            shape.1
        );
    }
    body.push_str("    end\n");

    // Service boundary nodes (only the ones the model actually reaches).
    if !model.services.is_empty() {
        body.push_str("    subgraph services [\"Service boundaries\"]\n");
        for service in &model.services {
            let _ = writeln!(
                body,
                "        {}[(\"{}\")]",
                service_node_id(service.kind),
                mermaid_label(&service.name)
            );
        }
        body.push_str("    end\n");
    }

    // Crate -> crate dependency edges.
    for Edge { from, to } in &model.edges {
        let _ = writeln!(body, "    {} --> {}", node_id(from), node_id(to));
    }

    // Crate -> service edges, attributed from each boundary's `pulled_in_by`
    // provenance so the arrow originates from the crate that pulls it in. The
    // always-on boundaries (daemon_url resolver, ghook inbox) that name a
    // crate are linked too; "workspace (...)" provenance has no crate node and
    // is left unlinked (the node still shows in the services subgraph).
    let crate_names: BTreeSet<&str> = model.crates.iter().map(|c| c.name.as_str()).collect();
    let mut service_edges: BTreeSet<(String, ServiceKind)> = BTreeSet::new();
    for service in &model.services {
        for provenance in &service.pulled_in_by {
            if let Some(name) = provenance_crate(provenance, &crate_names) {
                service_edges.insert((name.to_string(), service.kind));
            }
        }
    }
    for (krate, kind) in &service_edges {
        let _ = writeln!(
            body,
            "    {} -.->|\"{}\"| {}",
            node_id(krate),
            service_edge_label(*kind),
            service_node_id(*kind)
        );
    }

    // Standalone-vs-daemon runtime branch. Both modes are always present in
    // the model; draw the decision as a small annotated branch so the page
    // shows the AI-routing fork without inventing components.
    if has_mode(model, RuntimeMode::Standalone) || has_mode(model, RuntimeMode::DaemonAttached) {
        body.push_str("    subgraph runtime [\"Runtime routing\"]\n");
        body.push_str("        cli{{\"AI routing decision\"}}\n");
        if has_mode(model, RuntimeMode::Standalone) {
            body.push_str(
                "        cli -->|standalone| standalone[\"Direct to datastores / API\"]\n",
            );
        }
        if has_mode(model, RuntimeMode::DaemonAttached) {
            body.push_str("        cli -->|daemon| daemonmode[\"Delegate to Gobby daemon\"]\n");
        }
        body.push_str("    end\n");
    }

    // Boundary styling so services read distinctly from crates.
    body.push_str("    classDef service fill:#eef,stroke:#557,stroke-width:1px;\n");
    for service in &model.services {
        let _ = writeln!(body, "    class {} service;", service_node_id(service.kind));
    }

    let block = fence(&body);
    is_valid_mermaid(&block).then_some(block)
}

/// Runtime-flow sequence diagram. Picks the most expressive flow the model can
/// actually support:
///
/// * If the `ai` feature pulls in the daemon/embedding boundaries, render the
///   AI-generation routing flow (CLI → gcore → daemon|embedding API).
/// * Else if a ghook inbox boundary is present, render the enqueue-first hook
///   transport (ghook → inbox → daemon).
///
/// Returns `None` when the model expresses neither flow, so no sequence is
/// fabricated.
fn render_runtime_flow_sequence(model: &SystemModel) -> Option<String> {
    if let Some(block) = render_ai_generation_flow(model) {
        return Some(block);
    }
    render_ghook_enqueue_flow(model)
}

/// AI-generation routing sequence, seeded from the `ai`-feature service
/// boundaries (EmbeddingApi + Daemon). Only drawn when those boundaries exist.
fn render_ai_generation_flow(model: &SystemModel) -> Option<String> {
    let has_embedding = model
        .services
        .iter()
        .any(|s| s.kind == ServiceKind::EmbeddingApi);
    let has_daemon = model.services.iter().any(|s| s.kind == ServiceKind::Daemon);
    if !has_embedding || !has_daemon {
        return None;
    }

    // Name a concrete AI-feature consumer crate as the actor, falling back to
    // a generic "CLI" only if the model somehow has no such crate.
    let actor = ai_feature_crate(model).unwrap_or("CLI");

    let mut body = String::from("sequenceDiagram\n");
    let _ = writeln!(body, "    participant CLI as {}", mermaid_label(actor));
    body.push_str("    participant Core as gobby-core (ai_context)\n");
    body.push_str("    participant Daemon as Gobby daemon\n");
    body.push_str("    participant API as Embedding API\n");
    body.push_str("    CLI->>Core: resolve AiContext + routing decision\n");
    body.push_str("    alt daemon-attached\n");
    body.push_str("        Core->>Daemon: delegate generation\n");
    body.push_str("        Daemon-->>Core: completion\n");
    body.push_str("    else standalone (direct)\n");
    body.push_str("        Core->>API: embed / complete (OpenAI-compatible)\n");
    body.push_str("        API-->>Core: vectors / completion\n");
    body.push_str("    end\n");
    body.push_str("    Core-->>CLI: grounded result\n");

    let block = fence(&body);
    is_valid_mermaid(&block).then_some(block)
}

/// Enqueue-first hook transport sequence, seeded from the always-present
/// `GhookInbox` boundary (ghook → inbox → daemon). Drawn only when that
/// boundary is present in the model.
fn render_ghook_enqueue_flow(model: &SystemModel) -> Option<String> {
    let has_inbox = model
        .services
        .iter()
        .any(|s| s.kind == ServiceKind::GhookInbox);
    if !has_inbox {
        return None;
    }
    let has_daemon = model.services.iter().any(|s| s.kind == ServiceKind::Daemon);

    let mut body = String::from("sequenceDiagram\n");
    body.push_str("    participant Hook as ghook\n");
    body.push_str("    participant Inbox as ~/.gobby/hooks/inbox\n");
    if has_daemon {
        body.push_str("    participant Daemon as Gobby daemon\n");
    }
    body.push_str("    Hook->>Inbox: enqueue hook envelope\n");
    body.push_str("    Inbox-->>Hook: durable (survives daemon down)\n");
    if has_daemon {
        body.push_str("    Hook->>Daemon: best-effort POST\n");
        body.push_str("    Daemon-->>Hook: ack (optional)\n");
    }

    let block = fence(&body);
    is_valid_mermaid(&block).then_some(block)
}

/// True when `mode` is one of the model's runtime modes.
fn has_mode(model: &SystemModel, mode: RuntimeMode) -> bool {
    model.runtime_modes.contains(&mode)
}

/// First crate (in deterministic order) that pulls the `ai` feature into
/// `gobby-core`, used as the AI-generation actor.
fn ai_feature_crate(model: &SystemModel) -> Option<&str> {
    model
        .features_by_crate
        .iter()
        .find(|(_, feats)| feats.iter().any(|f| f == "ai"))
        .map(|(name, _)| name.as_str())
}

/// Extract the crate name from a `ServiceBoundary::pulled_in_by` provenance
/// string of the form `crate-name (feature: x)` or `crate-name (always)`,
/// returning it only when it names a known workspace crate. `"workspace (...)"`
/// provenance has no crate node and yields `None`.
fn provenance_crate<'a>(provenance: &'a str, crate_names: &BTreeSet<&str>) -> Option<&'a str> {
    let name = provenance.split(" (").next()?.trim();
    crate_names.contains(name).then_some(name)
}

/// Stable Mermaid node id for a crate: alphanumerics preserved, everything
/// else collapsed to `_`, prefixed so an all-digit/empty name is still a legal
/// identifier.
fn node_id(name: &str) -> String {
    let mut out = String::from("c_");
    for ch in name.chars() {
        if ch.is_ascii_alphanumeric() {
            out.push(ch);
        } else {
            out.push('_');
        }
    }
    out
}

/// Stable, collision-free Mermaid node id for a service boundary kind.
fn service_node_id(kind: ServiceKind) -> &'static str {
    match kind {
        ServiceKind::Postgres => "svc_postgres",
        ServiceKind::Falkor => "svc_falkor",
        ServiceKind::Qdrant => "svc_qdrant",
        ServiceKind::EmbeddingApi => "svc_embedding",
        ServiceKind::Daemon => "svc_daemon",
        ServiceKind::GhookInbox => "svc_inbox",
        ServiceKind::TreeSitter => "svc_treesitter",
        ServiceKind::DocumentToolchain => "svc_documents",
        ServiceKind::MediaToolchain => "svc_media",
    }
}

/// Short, fixed edge label classifying how the workspace depends on a service
/// boundary. Stitched onto the deterministic crate→service edges so the diagram
/// reads as an evaluator's dependency map, not just a wiring chart. The wording
/// is hard-coded (never LLM-drawn): the PostgreSQL hub is `required`;
/// FalkorDB/Qdrant/the embedding API are `degraded-ok` — required product
/// infrastructure with degraded command behavior when absent; the daemon is
/// `optional` routing; the ghook inbox is `always`-on transport; the parsing /
/// media boundaries are `toolchain` dependencies.
fn service_edge_label(kind: ServiceKind) -> &'static str {
    match kind {
        ServiceKind::Postgres => "required",
        ServiceKind::Falkor | ServiceKind::Qdrant | ServiceKind::EmbeddingApi => "degraded-ok",
        ServiceKind::Daemon => "optional",
        ServiceKind::GhookInbox => "always",
        ServiceKind::TreeSitter | ServiceKind::DocumentToolchain | ServiceKind::MediaToolchain => {
            "toolchain"
        }
    }
}

/// Escape a label for use inside a Mermaid `["..."]` node so brackets, quotes,
/// and pipes cannot break the surrounding syntax.
fn mermaid_label(text: &str) -> String {
    text.replace('\\', "\\\\")
        .replace('"', "&quot;")
        .replace('[', "&#91;")
        .replace(']', "&#93;")
        .replace('(', "&#40;")
        .replace(')', "&#41;")
        .replace('{', "&#123;")
        .replace('}', "&#125;")
        .replace('|', "&#124;")
}

/// Wrap a diagram body in a ```` ```mermaid ```` fence with a trailing newline.
fn fence(body: &str) -> String {
    let trimmed = body.trim_end_matches('\n');
    format!("```mermaid\n{trimmed}\n```\n")
}

/// Hand-written well-formedness gate for a single ```` ```mermaid ```` block.
///
/// A block passes only when ALL of these hold:
///
/// * It opens with a ```` ```mermaid ```` fence line and closes with a ```` ``` ````
///   fence line (the fence is balanced and properly closed).
/// * The first non-empty line inside the fence is a recognised diagram header
///   ([`VALID_HEADERS`]).
/// * There is at least one content line after the header (the diagram is not
///   empty).
/// * No interior line opens another fence (no nested/un-terminated fences).
/// * Bracket/paren/brace delimiters across the body are balanced, so no node
///   shape is left half-open.
///
/// This is intentionally conservative: it rejects anything it cannot prove
/// well-formed rather than risk emitting a fence a Markdown/Mermaid renderer
/// would choke on.
pub(crate) fn is_valid_mermaid(block: &str) -> bool {
    let lines: Vec<&str> = block.lines().collect();
    if lines.len() < 3 {
        // Need at minimum: opening fence, a header, a closing fence.
        return false;
    }
    if lines[0].trim() != "```mermaid" {
        return false;
    }
    // Exactly one closing fence, and it is the last non-empty line.
    let Some(close_idx) = lines.iter().rposition(|l| l.trim() == "```") else {
        return false;
    };
    if close_idx == 0 {
        return false;
    }
    // No stray fence markers between the open and the close.
    if lines[1..close_idx]
        .iter()
        .any(|l| l.trim_start().starts_with("```"))
    {
        return false;
    }
    // Anything after the closing fence must be blank.
    if lines[close_idx + 1..].iter().any(|l| !l.trim().is_empty()) {
        return false;
    }

    let interior = &lines[1..close_idx];
    let mut content = interior.iter().filter(|l| !l.trim().is_empty());
    let Some(header) = content.next() else {
        return false;
    };
    let header = header.trim();
    if !VALID_HEADERS.iter().any(|h| header.starts_with(h)) {
        return false;
    }
    // At least one content line beyond the header.
    if content.next().is_none() {
        return false;
    }

    // Delimiters across the interior must balance (cheap structural check that
    // catches a half-open node like `a["b` or an unterminated subgraph node).
    balanced_delimiters(interior)
}

/// True when `(`/`)`, `[`/`]`, and `{`/`}` are balanced across the lines, with
/// quoted spans skipped so punctuation inside a `"..."` label does not count.
fn balanced_delimiters(lines: &[&str]) -> bool {
    let (mut paren, mut bracket, mut brace) = (0i32, 0i32, 0i32);
    let mut in_quote = false;
    for line in lines {
        for ch in line.chars() {
            if ch == '"' {
                in_quote = !in_quote;
                continue;
            }
            if in_quote {
                continue;
            }
            match ch {
                '(' => paren += 1,
                ')' => paren -= 1,
                '[' => bracket += 1,
                ']' => bracket -= 1,
                '{' => brace += 1,
                '}' => brace -= 1,
                _ => {}
            }
            if paren < 0 || bracket < 0 || brace < 0 {
                return false;
            }
        }
        // A label quote never spans lines in our generated diagrams.
        if in_quote {
            return false;
        }
    }
    paren == 0 && bracket == 0 && brace == 0
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::commands::codewiki::system_model::{Crate, ServiceBoundary};
    use std::collections::BTreeMap;

    /// A realistic three-binary + foundation model resembling the real
    /// workspace: gobby-code/gobby-wiki/gobby-hooks all depend on gobby-core,
    /// gobby-code pulls postgres + ai, so EmbeddingApi/Daemon/Postgres/inbox
    /// boundaries are present and both runtime modes exist.
    fn sample_model() -> SystemModel {
        let krate = |name: &str, path: &str, bin: bool, lib: bool| Crate {
            name: name.to_string(),
            path: path.to_string(),
            is_binary: bin,
            is_lib: lib,
        };
        let edge = |from: &str, to: &str| Edge {
            from: from.to_string(),
            to: to.to_string(),
        };
        let boundary = |name: &str, kind: ServiceKind, pulled: &[&str]| ServiceBoundary {
            name: name.to_string(),
            kind,
            pulled_in_by: pulled.iter().map(|s| s.to_string()).collect(),
        };

        let mut features_by_crate = BTreeMap::new();
        features_by_crate.insert(
            "gobby-code".to_string(),
            vec!["ai".to_string(), "postgres".to_string()],
        );

        SystemModel {
            crates: vec![
                krate("gobby-code", "crates/gcode", true, false),
                krate("gobby-core", "crates/gcore", false, true),
                krate("gobby-hooks", "crates/ghook", true, false),
                krate("gobby-wiki", "crates/gwiki", true, false),
            ],
            edges: vec![
                edge("gobby-code", "gobby-core"),
                edge("gobby-hooks", "gobby-core"),
                edge("gobby-wiki", "gobby-core"),
            ],
            services: vec![
                boundary(
                    "PostgreSQL hub",
                    ServiceKind::Postgres,
                    &["gobby-code (feature: postgres)"],
                ),
                boundary(
                    "Embedding API",
                    ServiceKind::EmbeddingApi,
                    &["gobby-code (feature: ai)"],
                ),
                boundary(
                    "Gobby daemon",
                    ServiceKind::Daemon,
                    &[
                        "gobby-code (feature: ai)",
                        "workspace (gobby_core::daemon_url, always)",
                    ],
                ),
                boundary(
                    "ghook inbox",
                    ServiceKind::GhookInbox,
                    &["gobby-hooks (always)"],
                ),
            ],
            runtime_modes: vec![RuntimeMode::Standalone, RuntimeMode::DaemonAttached],
            features_by_crate,
            notes: Vec::new(),
        }
    }

    #[test]
    fn topology_diagram_contains_crate_nodes_and_dependency_edges() {
        let model = sample_model();
        let block = render_topology_flowchart(&model).expect("topology rendered for full model");

        // Well-formed per the validator.
        assert!(is_valid_mermaid(&block), "topology must be valid:\n{block}");
        assert!(block.starts_with("```mermaid\n"));
        assert!(block.trim_end().ends_with("```"));

        // Recognised flowchart header.
        assert!(block.contains("flowchart TD"));

        // Every crate name appears as a node label.
        for name in ["gobby-code", "gobby-core", "gobby-wiki", "gobby-hooks"] {
            assert!(
                block.contains(name),
                "missing crate node `{name}`:\n{block}"
            );
        }

        // Dependency edge gobby-code -> gobby-core is drawn (by sanitized id).
        let from = node_id("gobby-code");
        let to = node_id("gobby-core");
        assert!(
            block.contains(&format!("{from} --> {to}")),
            "missing edge gobby-code -> gobby-core:\n{block}"
        );

        // Service boundary nodes are present and styled distinctly.
        assert!(block.contains("PostgreSQL hub"));
        assert!(block.contains("classDef service"));

        // The standalone-vs-daemon runtime branch is represented.
        assert!(block.contains("standalone"));
        assert!(block.contains("daemon"));
    }

    #[test]
    fn service_edges_carry_fixed_dependency_strength_labels() {
        let model = sample_model();
        let block = render_topology_flowchart(&model).expect("topology rendered for full model");
        assert!(is_valid_mermaid(&block), "labelled topology must stay valid:\n{block}");

        let code = node_id("gobby-code");
        // The PostgreSQL hub edge is hard-`required`; the embedding API edge is
        // `degraded-ok`. Both labels are fixed, never LLM-drawn.
        assert!(
            block.contains(&format!("{code} -.->|\"required\"| {}", service_node_id(ServiceKind::Postgres))),
            "postgres edge must be labelled required:\n{block}"
        );
        assert!(
            block.contains(&format!(
                "{code} -.->|\"degraded-ok\"| {}",
                service_node_id(ServiceKind::EmbeddingApi)
            )),
            "embedding edge must be labelled degraded-ok:\n{block}"
        );
    }

    #[test]
    fn diagram_section_caption_explains_required_but_degraded_framing() {
        let model = sample_model();
        let section = render_architecture_diagrams(&model).expect("section for full model");
        assert!(
            section.contains("required product infrastructure with degraded command behavior"),
            "caption must carry the required-but-degraded framing:\n{section}"
        );
    }

    #[test]
    fn service_matrix_lists_services_with_fixed_requirement_classes() {
        let model = sample_model();
        let matrix = render_service_matrix(&model).expect("matrix for full model");
        assert!(matrix.contains("## Services"));
        assert!(matrix.contains("| Service | Requirement | Pulled in by |"));
        assert!(matrix.contains("PostgreSQL hub"));
        assert!(matrix.contains("Required (index-backed commands)"));
        // The embedding API boundary is required-but-degraded.
        assert!(matrix.contains("Required, degraded behavior when absent"));
        assert!(
            matrix.contains("required product infrastructure with degraded command behavior"),
            "matrix intro must carry the required-but-degraded framing:\n{matrix}"
        );
    }

    #[test]
    fn service_matrix_empty_when_model_reaches_no_services() {
        let mut model = sample_model();
        model.services.clear();
        assert!(render_service_matrix(&model).is_none());
    }

    #[test]
    fn runtime_flow_sequence_is_valid_and_model_seeded() {
        let model = sample_model();
        let block = render_runtime_flow_sequence(&model).expect("ai flow for ai-feature model");
        assert!(is_valid_mermaid(&block), "sequence must be valid:\n{block}");
        assert!(block.contains("sequenceDiagram"));
        // The AI-feature crate is the named actor.
        assert!(block.contains("gobby-code"));
        assert!(block.contains("Gobby daemon"));
        assert!(block.contains("Embedding API"));
    }

    #[test]
    fn ghook_flow_used_when_no_ai_boundary() {
        // A model with only the always-on inbox + daemon (no ai feature) falls
        // back to the enqueue-first hook transport sequence.
        let mut model = sample_model();
        model.features_by_crate.clear();
        model
            .services
            .retain(|s| s.kind == ServiceKind::GhookInbox || s.kind == ServiceKind::Daemon);
        let block = render_runtime_flow_sequence(&model).expect("ghook flow present");
        assert!(is_valid_mermaid(&block), "{block}");
        assert!(block.contains("ghook"));
        assert!(block.contains("inbox"));
    }

    #[test]
    fn empty_model_draws_nothing_but_is_not_an_error() {
        let model = SystemModel {
            crates: Vec::new(),
            edges: Vec::new(),
            services: Vec::new(),
            runtime_modes: vec![RuntimeMode::Standalone, RuntimeMode::DaemonAttached],
            features_by_crate: BTreeMap::new(),
            notes: vec!["cannot read workspace manifest".to_string()],
        };
        // Topology omitted (no crates), runtime flow omitted (no boundaries),
        // and the whole section is therefore absent — without any panic.
        assert!(render_topology_flowchart(&model).is_none());
        assert!(render_runtime_flow_sequence(&model).is_none());
        assert!(render_architecture_diagrams(&model).is_none());
    }

    #[test]
    fn section_render_includes_prose_and_only_valid_fences() {
        let model = sample_model();
        let section = render_architecture_diagrams(&model).expect("section rendered");
        assert!(section.contains("## Architecture Diagrams"));
        assert!(section.contains("derived from the workspace"));
        // Every mermaid fence in the section is balanced and individually valid.
        let fences: Vec<&str> = section
            .match_indices("```mermaid")
            .map(|(_, s)| s)
            .collect();
        assert!(fences.len() >= 2, "expected topology + flow fences");
        // Count of opening fences equals count of closing fences.
        let opens = section.matches("```mermaid").count();
        let total_fences = section.matches("```").count();
        assert_eq!(total_fences, opens * 2, "every fence is closed:\n{section}");
    }

    #[test]
    fn validator_accepts_minimal_flowchart() {
        let block = "```mermaid\nflowchart TD\n    a --> b\n```\n";
        assert!(is_valid_mermaid(block));
    }

    #[test]
    fn validator_accepts_sequence_diagram() {
        let block = "```mermaid\nsequenceDiagram\n    A->>B: msg\n    B-->>A: reply\n```\n";
        assert!(is_valid_mermaid(block));
    }

    #[test]
    fn validator_rejects_unrecognized_header() {
        let block = "```mermaid\nbananas\n    a --> b\n```\n";
        assert!(!is_valid_mermaid(block));
    }

    #[test]
    fn validator_rejects_unclosed_fence() {
        let block = "```mermaid\nflowchart TD\n    a --> b\n";
        assert!(!is_valid_mermaid(block));
    }

    #[test]
    fn validator_rejects_empty_diagram() {
        // Header but no content line.
        let block = "```mermaid\nflowchart TD\n```\n";
        assert!(!is_valid_mermaid(block));
    }

    #[test]
    fn validator_rejects_unbalanced_node_shape() {
        // `a["b` leaves an open bracket — must be rejected.
        let block = "```mermaid\nflowchart TD\n    a[\"b --> c\n```\n";
        assert!(!is_valid_mermaid(block));
    }

    #[test]
    fn validator_rejects_nested_fence() {
        let block = "```mermaid\nflowchart TD\n```mermaid\n    a --> b\n```\n";
        assert!(!is_valid_mermaid(block));
    }

    #[test]
    fn validator_rejects_content_after_close() {
        let block = "```mermaid\nflowchart TD\n    a --> b\n```\nstray text\n";
        assert!(!is_valid_mermaid(block));
    }
}
