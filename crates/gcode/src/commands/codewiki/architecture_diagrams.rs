//! Architecture-page diagram evidence for the codewiki (#891, #17521).
//!
//! Since #17521 this module is an *evidence supplier + validator*, not a
//! composer: it reduces the deterministic [`SystemModel`] built by
//! [`super::system_model::build_system_model`] to a [`DiagramEvidence`] graph
//! — crate nodes, workspace-internal dependency edges, service boundaries
//! with fixed dependency-strength labels, and the standalone-vs-daemon
//! runtime branch — and hands it to the LLM composer in
//! [`super::diagram_compose`]. The model picks the story; deterministic code
//! verifies every drawn arrow against this evidence (unevidenced arrows are
//! rejected), re-renders the syntax, and gates the block.
//!
//! These are *architectural* diagrams seeded strictly from workspace facts on
//! disk, NOT the per-symbol FalkorDB call/import-edge dumps that commit #884
//! deliberately removed. The model draws only what the evidence supplies;
//! nothing here invents components absent from the model.
//!
//! Invariants (the #884 / #878 / #17521 contract):
//!
//! * **Edge verification.** Every arrow in the emitted diagram matches a
//!   supplied evidence edge — the diagram analog of citation grounding.
//! * **Valid-Mermaid gate.** Every block passes the shared gobby-core
//!   `is_valid_mermaid` gate before it is emitted; a broken fence is never
//!   written.
//! * **No disconnected islands.** The composer keeps only the largest
//!   weakly-connected component, and the runtime-routing branch is anchored
//!   to the ai-feature crate (or omitted) rather than left floating.
//! * **Non-degrading.** A SystemModel too sparse to draw, an AI-off run, or a
//!   composition that fails verification yields no diagram, and that is
//!   *normal*, not degradation.

use std::collections::BTreeSet;
use std::fmt::Write as _;

use super::CodewikiProgress;
use super::diagram_compose::{
    DiagramEvidence, DiagramKind, DiagramOutcome, DiagramStats, NodeShape, compose_flowchart,
};
use super::system_model::{Edge, RuntimeMode, ServiceKind, SystemModel};
use super::types::TextGenerator;

/// Render the architecture diagram section for the page body: a leading prose
/// note, then the LLM-composed, edge-verified topology flowchart. Returns
/// `None` when the model is too sparse to draw, no generator is available
/// (AI off), or nothing survived verification — the caller then simply omits
/// the section. A returned string always ends with a trailing blank line and
/// contains only validated fences.
pub(crate) fn render_architecture_diagrams(
    model: &SystemModel,
    generate: &mut Option<&mut TextGenerator<'_>>,
    diagram_stats: &mut DiagramStats,
    progress: &mut CodewikiProgress,
) -> Option<String> {
    let evidence = architecture_diagram_evidence(model);
    let outcome = compose_flowchart(generate, &evidence, "the workspace architecture topology");
    diagram_stats.record(
        "code/_architecture.md",
        DiagramKind::CuratedFlow,
        &outcome,
        progress,
    );
    let DiagramOutcome::Emitted(block) = outcome else {
        return None;
    };

    let mut section = String::new();
    section.push_str("## Architecture Diagrams\n\n");
    section.push_str(
        "This diagram is composed by the model from evidence derived from the \
workspace `Cargo.toml` topology — member crates, their workspace-internal \
dependency edges, the service boundaries their feature gates pull in, and the \
standalone-vs-daemon runtime branch. Every arrow is verified against that \
evidence before the diagram is emitted; it describes structure, not \
per-symbol call graphs.\n\n",
    );
    section.push_str(
        "Solid edges are workspace-internal crate dependencies. Dotted edges are \
service boundaries, labelled by dependency strength: **required** (the command \
cannot run without it), **degraded-ok** (required product infrastructure with \
degraded command behavior when the service is absent), **optional** (engaged \
only when that routing is selected), and **always** (always-on transport).\n\n",
    );
    section.push_str(&block);
    if !section.ends_with('\n') {
        section.push('\n');
    }
    section.push('\n');
    Some(section)
}

/// Reduce the workspace [`SystemModel`] to the evidence graph the composer may
/// draw from: crates (binaries as stadiums), reached service boundaries
/// (cylinders), crate→crate dependency edges, crate→service edges labelled
/// with their fixed dependency strength, and the standalone-vs-daemon runtime
/// branch anchored to the ai-feature crate. Everything is a workspace fact;
/// nothing is invented.
pub(crate) fn architecture_diagram_evidence(model: &SystemModel) -> DiagramEvidence {
    let mut evidence = DiagramEvidence::default();

    for krate in &model.crates {
        let shape = if krate.is_binary && !krate.is_lib {
            // Binaries read as runnable entry points.
            NodeShape::Stadium
        } else {
            NodeShape::Box
        };
        evidence.push_node(node_id(&krate.name), krate.name.clone(), shape);
    }
    let crate_names: BTreeSet<&str> = model.crates.iter().map(|c| c.name.as_str()).collect();

    // Service boundary nodes (only the ones the model actually reaches).
    for service in &model.services {
        evidence.push_node(
            service_node_id(service.kind),
            service.name.clone(),
            NodeShape::Cylinder,
        );
    }

    // Crate -> crate dependency edges.
    for Edge { from, to } in &model.edges {
        if crate_names.contains(from.as_str()) && crate_names.contains(to.as_str()) {
            evidence.push_edge(node_id(from), node_id(to), None, false);
        }
    }

    // Crate -> service edges, attributed from each boundary's `pulled_in_by`
    // provenance so the arrow originates from the crate that pulls it in.
    // "workspace (...)" provenance has no crate node and is left unlinked (the
    // node still shows when another crate reaches it).
    for service in &model.services {
        for provenance in &service.pulled_in_by {
            if let Some(name) = provenance_crate(provenance, &crate_names) {
                evidence.push_edge(
                    node_id(name),
                    service_node_id(service.kind),
                    Some(service_edge_label(service.kind).to_string()),
                    true,
                );
            }
        }
    }

    // Standalone-vs-daemon runtime branch, anchored to the crate that makes
    // the routing decision (the ai-feature consumer) so the branch joins the
    // crate topology instead of floating as a disconnected island. When no
    // model crate pulls the `ai` feature there is nothing real to anchor to,
    // so the fork is omitted rather than fabricated.
    let standalone = has_mode(model, RuntimeMode::Standalone);
    let daemon_attached = has_mode(model, RuntimeMode::DaemonAttached);
    let routing_crate = ai_feature_crate(model).filter(|name| crate_names.contains(name));
    if let Some(krate) = routing_crate
        && (standalone || daemon_attached)
    {
        evidence.push_node("cli", "AI routing decision", NodeShape::Box);
        evidence.push_edge(node_id(krate), "cli", None, false);
        if standalone {
            evidence.push_node("standalone", "Direct to datastores / API", NodeShape::Box);
            evidence.push_edge("cli", "standalone", Some("standalone".to_string()), false);
        }
        if daemon_attached {
            evidence.push_node("daemonmode", "Delegate to Gobby daemon", NodeShape::Box);
            evidence.push_edge("cli", "daemonmode", Some("daemon".to_string()), false);
            // The daemon-mode leaf delegates to the daemon service boundary
            // when the model reaches one, closing the branch into the graph.
            if model.services.iter().any(|s| s.kind == ServiceKind::Daemon) {
                evidence.push_edge(
                    "daemonmode",
                    service_node_id(ServiceKind::Daemon),
                    None,
                    true,
                );
            }
        }
    }

    evidence
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
/// boundary. Stitched onto the crate→service evidence edges so the diagram
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

#[cfg(test)]
mod tests {
    use super::super::types::PromptTier;
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

    /// Scripted composer that draws every supplied evidence edge — the
    /// obedient-model baseline that exercises the full verify/normalize
    /// pipeline.
    fn draw_all_edges(evidence: &DiagramEvidence) -> String {
        let mut body = String::from("flowchart TD\n");
        for edge in &evidence.edges {
            body.push_str(&format!("    {} --> {}\n", edge.from, edge.to));
        }
        body
    }

    fn compose_section(model: &SystemModel, responses: Vec<String>) -> Option<String> {
        let mut responses = responses;
        let mut generator = move |_prompt: &str, _system: &str, _tier: PromptTier| {
            (!responses.is_empty()).then(|| responses.remove(0))
        };
        let mut generate: Option<&mut TextGenerator<'_>> = Some(&mut generator);
        let mut stats = DiagramStats::default();
        let mut progress = CodewikiProgress::silent();
        render_architecture_diagrams(model, &mut generate, &mut stats, &mut progress)
    }

    #[test]
    fn evidence_contains_crate_nodes_dependency_and_service_edges() {
        let model = sample_model();
        let evidence = architecture_diagram_evidence(&model);

        let ids: Vec<&str> = evidence.nodes.iter().map(|n| n.id.as_str()).collect();
        for name in ["gobby-code", "gobby-core", "gobby-wiki", "gobby-hooks"] {
            assert!(
                ids.contains(&node_id(name).as_str()),
                "missing crate node {name}"
            );
        }
        assert!(ids.contains(&"svc_postgres"));

        // Dependency edge gobby-code -> gobby-core is evidenced, solid.
        assert!(evidence.edges.iter().any(|edge| {
            edge.from == node_id("gobby-code") && edge.to == node_id("gobby-core") && !edge.dotted
        }));
        // Service edges carry the fixed dependency-strength labels, dotted.
        assert!(evidence.edges.iter().any(|edge| {
            edge.from == node_id("gobby-code")
                && edge.to == "svc_postgres"
                && edge.dotted
                && edge.label.as_deref() == Some("required")
        }));
        assert!(evidence.edges.iter().any(|edge| {
            edge.to == "svc_embedding" && edge.label.as_deref() == Some("degraded-ok")
        }));
        // Runtime branch is anchored to the ai-feature crate and closes into
        // the daemon boundary.
        assert!(
            evidence
                .edges
                .iter()
                .any(|edge| edge.from == node_id("gobby-code") && edge.to == "cli")
        );
        assert!(
            evidence
                .edges
                .iter()
                .any(|edge| edge.from == "daemonmode" && edge.to == "svc_daemon")
        );
    }

    #[test]
    fn runtime_routing_omitted_without_an_ai_feature_crate() {
        // With no crate pulling the `ai` feature there is nothing real to
        // anchor the routing fork to, so it is not evidenced at all.
        let mut model = sample_model();
        model.features_by_crate.clear();
        let evidence = architecture_diagram_evidence(&model);
        assert!(evidence.nodes.iter().all(|node| node.id != "cli"));
        assert!(evidence.edges.iter().all(|edge| edge.to != "cli"));
    }

    #[test]
    fn composed_section_carries_caption_and_verified_topology() {
        let model = sample_model();
        let evidence = architecture_diagram_evidence(&model);
        let section =
            compose_section(&model, vec![draw_all_edges(&evidence)]).expect("section rendered");

        assert!(section.contains("## Architecture Diagrams"));
        assert!(section.contains("composed by the model"));
        assert!(
            section.contains("required product infrastructure with degraded command behavior"),
            "caption must carry the required-but-degraded framing:\n{section}"
        );
        // Crate labels and the canonical service edge styling survive
        // normalization.
        assert!(section.contains("gobby-code"));
        assert!(section.contains("PostgreSQL hub"));
        assert!(
            section.contains(&format!(
                "{} -.->|\"required\"| svc_postgres",
                node_id("gobby-code")
            )),
            "postgres edge must be dotted and labelled required:\n{section}"
        );
        // Exactly one balanced fence.
        assert_eq!(section.matches("```mermaid").count(), 1);
        assert_eq!(section.matches("```").count(), 2);
    }

    #[test]
    fn unevidenced_arrow_never_reaches_the_page() {
        let model = sample_model();
        // The model invents a reversed dependency; edge verification must
        // reject it while keeping the evidenced remainder.
        let mut drawing = draw_all_edges(&architecture_diagram_evidence(&model));
        drawing.push_str(&format!(
            "    {} --> {}\n",
            node_id("gobby-core"),
            node_id("gobby-code")
        ));
        let section =
            compose_section(&model, vec![drawing.clone(), drawing]).expect("section rendered");
        assert!(
            !section.contains(&format!(
                "{} --> {}",
                node_id("gobby-core"),
                node_id("gobby-code")
            )),
            "reversed dependency must be rejected:\n{section}"
        );
    }

    #[test]
    fn topology_output_has_no_disconnected_islands() {
        // An isolated crate (no dependency edges, no service pulls) is real
        // evidence but has no evidenced arrows; whatever the model draws, the
        // emitted diagram never contains an island.
        let mut model = sample_model();
        model.crates.push(Crate {
            name: "gobby-floater".to_string(),
            path: "crates/floater".to_string(),
            is_binary: false,
            is_lib: true,
        });
        let evidence = architecture_diagram_evidence(&model);
        let section =
            compose_section(&model, vec![draw_all_edges(&evidence)]).expect("section rendered");
        assert!(
            !section.contains("gobby-floater"),
            "edge-less crate must not appear as an island:\n{section}"
        );
    }

    #[test]
    fn diagram_outcomes_record_no_generator_and_sparse_architecture_slots() {
        let model = sample_model();
        let mut generate: Option<&mut TextGenerator<'_>> = None;
        let mut stats = DiagramStats::default();
        let mut progress = CodewikiProgress::capture();
        assert!(
            render_architecture_diagrams(&model, &mut generate, &mut stats, &mut progress,)
                .is_none()
        );
        assert_eq!(stats.no_generator, 1);
        assert_eq!(
            progress.into_lines(),
            vec!["codewiki: diagram code/_architecture.md [curated_flow]: no_generator"]
        );

        let empty = SystemModel {
            crates: Vec::new(),
            edges: Vec::new(),
            services: Vec::new(),
            runtime_modes: vec![RuntimeMode::Standalone, RuntimeMode::DaemonAttached],
            features_by_crate: BTreeMap::new(),
            notes: vec!["cannot read workspace manifest".to_string()],
        };
        assert!(architecture_diagram_evidence(&empty).is_sparse());
        let mut empty_generate: Option<&mut TextGenerator<'_>> = None;
        let mut empty_stats = DiagramStats::default();
        let mut empty_progress = CodewikiProgress::silent();
        assert!(
            render_architecture_diagrams(
                &empty,
                &mut empty_generate,
                &mut empty_stats,
                &mut empty_progress,
            )
            .is_none()
        );
        assert_eq!(empty_stats.sparse_evidence, 1);
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

    /// RC4 gate (#17499): every Mermaid block the composition pipeline can
    /// emit must pass the REAL Mermaid parser, not just the structural
    /// `is_valid_mermaid` check. Runs `npx -y @mermaid-js/mermaid-cli` over
    /// one markdown document holding representative composed blocks; mmdc
    /// exits non-zero if any block fails to parse. Skips (with a note) only
    /// when the CLI itself cannot be resolved, so offline environments do not
    /// fail spuriously.
    #[test]
    fn emitted_mermaid_blocks_pass_real_mermaid_parser() {
        let probe = std::process::Command::new("npx")
            .args(["-y", "@mermaid-js/mermaid-cli", "--version"])
            .output();
        match probe {
            Ok(out) if out.status.success() => {}
            _ => {
                eprintln!("skipping: npx / @mermaid-js/mermaid-cli unavailable");
                return;
            }
        }

        let mut doc = String::new();

        // Architecture section: the full evidence graph drawn by an obedient
        // scripted model, normalized by the pipeline (all shapes, dotted
        // labelled service edges, runtime branch).
        let model = sample_model();
        let evidence = architecture_diagram_evidence(&model);
        let section =
            compose_section(&model, vec![draw_all_edges(&evidence)]).expect("section rendered");
        doc.push_str(&section);

        // A conceptual-flow style evidence graph with every escaped character
        // class in its labels, composed through the same pipeline.
        let mut nasty = DiagramEvidence::default();
        nasty.push_node(
            "s0",
            "walker (fs) — discovers [candidate] files",
            NodeShape::Box,
        );
        nasty.push_node(
            "s1",
            "parser {ts} — extracts the |AST| via \"tree-sitter\"",
            NodeShape::Box,
        );
        nasty.push_node(
            "s2",
            "indexer #1 — writes hub rows \\ chunks",
            NodeShape::Box,
        );
        nasty.push_edge("s0", "s1", None, false);
        nasty.push_edge("s1", "s2", Some("hands off".to_string()), false);
        let mut responses = vec!["flowchart LR\n    s0 --> s1\n    s1 --> s2\n".to_string()];
        let mut generator = move |_prompt: &str, _system: &str, _tier: PromptTier| {
            (!responses.is_empty()).then(|| responses.remove(0))
        };
        let mut generate: Option<&mut TextGenerator<'_>> = Some(&mut generator);
        let DiagramOutcome::Emitted(flow) =
            compose_flowchart(&mut generate, &nasty, "escaped-label flow")
        else {
            panic!("nasty flow was not emitted");
        };
        doc.push('\n');
        doc.push_str(&flow);

        let dir = tempfile::tempdir().expect("tempdir");
        let input = dir.path().join("emitted.md");
        let output = dir.path().join("emitted.out.md");
        let puppeteer = dir.path().join("puppeteer.json");
        std::fs::write(&input, &doc).expect("write emitted blocks");
        std::fs::write(
            &puppeteer,
            r#"{"args":["--no-sandbox","--disable-setuid-sandbox"]}"#,
        )
        .expect("write puppeteer config");

        let run = match std::process::Command::new("npx")
            .args(["-y", "@mermaid-js/mermaid-cli", "-i"])
            .arg(&input)
            .arg("-o")
            .arg(&output)
            .arg("-p")
            .arg(&puppeteer)
            .output()
        {
            Ok(run) => run,
            Err(error) => {
                eprintln!("skipping: failed to launch mmdc after probe succeeded: {error}");
                return;
            }
        };
        if !run.status.success() && is_chromium_launch_failure(&run.stderr) {
            eprintln!(
                "skipping: mmdc resolved but Chromium could not launch:\n{}",
                String::from_utf8_lossy(&run.stderr)
            );
            return;
        }
        assert!(
            run.status.success(),
            "mmdc rejected an emitted block:\n--- stderr ---\n{}\n--- blocks ---\n{doc}",
            String::from_utf8_lossy(&run.stderr)
        );
    }

    fn is_chromium_launch_failure(stderr: &[u8]) -> bool {
        let stderr = String::from_utf8_lossy(stderr).to_ascii_lowercase();
        stderr.contains("failed to launch the browser process")
            || stderr.contains("no usable sandbox")
            || stderr.contains("running as root without --no-sandbox")
            || stderr.contains("could not find chrome")
            || stderr.contains("could not find chromium")
    }
}
