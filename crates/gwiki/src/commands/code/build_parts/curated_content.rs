//! Per-page content pass for the curated navigation layer.
//!
//! The structure pass ([`super::concepts`]) names concepts and narrative
//! chapters and gives each a one-line summary. That alone renders thin (a
//! sentence wrapped in provenance). This module runs a second, per-page LLM
//! pass that expands each page into a grounded, multi-section body, with a
//! deterministic structural fallback so `--ai off` and generation failures
//! still produce real structure rather than a bare summary (#853).
//!
//! It is deliberately decoupled from the `ConceptModule`/`NarrativePage`
//! structs: callers pass the primitive member lists, and we hand back a body
//! string. That keeps `concepts.rs` under the 1,000-line rule while this file
//! owns the content-pass + fallback logic.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write as _;

use gobby_core::vault::mermaid::{escape_label as mermaid_label, is_valid_mermaid};

use super::super::*;

#[path = "curated_content/page_content.rs"]
mod page_content;
#[cfg(test)]
#[path = "curated_content/tests.rs"]
mod tests;
#[path = "curated_content/tool_loop_dump.rs"]
mod tool_loop_dump;

use page_content::{
    has_required_curated_sections, member_evidence_rows, structural_body, symbol_evidence_rows,
    verifier_evidence_rows,
};
pub(crate) use tool_loop_dump::resolve_tool_loop_dump_dir;

/// Which curated page voice to generate. Selects the system prompt and the
/// prompt builder: concept pages are reference explainers, narrative pages are
/// guided-tour chapters.
#[derive(Clone, Copy)]
pub(crate) enum CuratedPageKind {
    Concept,
    Narrative,
}

/// Outcome of the per-page content pass.
pub(crate) struct CuratedBody {
    /// The multi-section page body, ready to drop in after the page title.
    /// `None` only when the page has no member content to describe at all.
    pub(crate) body: Option<String>,
    /// Distinct degradation reason codes when a requested content pass fell back
    /// to structural prose: a refusal/echo/unavailable AI failure or a grounding
    /// gap, never a blanket `model-unavailable`. Empty when the body is a
    /// complete handbook body, or when `--ai off` skips leave healthy structural
    /// output.
    pub(crate) degraded_sources: Vec<String>,
    pub(crate) verify_notes: Vec<VerifyNote>,
    /// Per-page tool-loop observability, recorded into the page's
    /// frontmatter when the run used the tool loop (#978). Default (zero counts)
    /// for the one-shot / structural path.
    pub(crate) observability: GenerationObservability,
}

/// Run the content pass for one curated page.
///
/// `spans` are the page-specific spans used to ground the generated prose
/// (concept item spans / narrative spans), never the whole-input span set.
#[allow(clippy::too_many_arguments)]
pub(crate) fn curated_page_body(
    kind: CuratedPageKind,
    title: &str,
    summary: &str,
    member_modules: &[String],
    member_files: &[String],
    module_lookup: &BTreeMap<&str, &ModuleDoc>,
    file_lookup: &BTreeMap<&str, &FileDoc>,
    leading_chunks: &BTreeMap<String, LeadingChunk>,
    spans: &[SourceSpan],
    tool_loop_dump_dir: Option<&std::path::Path>,
    generate: &mut Option<&mut TextGenerator<'_>>,
    verify: &mut Option<&mut TextVerifier<'_>>,
) -> anyhow::Result<CuratedBody> {
    let members = member_evidence_rows(member_modules, member_files, module_lookup, file_lookup);
    let symbols = symbol_evidence_rows(member_files, file_lookup);
    if members.is_empty() && symbols.is_empty() {
        return Ok(CuratedBody {
            body: None,
            degraded_sources: Vec::new(),
            verify_notes: Vec::new(),
            observability: GenerationObservability::default(),
        });
    }

    let excerpt_take = match kind {
        CuratedPageKind::Concept => prompts::CONCEPT_PAGE_SOURCE_EXCERPTS,
        CuratedPageKind::Narrative => prompts::NARRATIVE_PAGE_SOURCE_EXCERPTS,
    };
    let member_file_docs = member_files
        .iter()
        .filter_map(|file| file_lookup.get(file.as_str()).copied());
    let sources = ranked_source_excerpts(member_file_docs, leading_chunks, excerpt_take);

    let prompt = match kind {
        CuratedPageKind::Concept => {
            prompts::concept_page_prompt(title, summary, &members, &symbols, &sources)
        }
        CuratedPageKind::Narrative => {
            prompts::narrative_page_prompt(title, summary, &members, &symbols, &sources)
        }
    };
    let system = match kind {
        CuratedPageKind::Concept => prompts::CONCEPT_PAGE_SYSTEM,
        CuratedPageKind::Narrative => prompts::NARRATIVE_PAGE_SYSTEM,
    };

    // Curated concept/narrative bodies use one-shot generation (gobby-cli #1001),
    // matching the curated navigation plan (#993): the page already carries
    // assembled member/symbol/source evidence in its prompt, so the multi-turn
    // The agentic tool loop only added serial cold-spawn latency and parse
    // instability without earning grounding. `tool_loop` is intentionally not
    // forwarded here; the tool loop stays for repo/architecture aggregates. An
    // empty/incomplete one-shot degrades to the structural body below.
    let aggregate = generate_aggregate(
        &mut None,
        generate,
        &prompt,
        system,
        &format!("curated page '{title}'"),
    )?;
    let observability = aggregate.observability.clone();
    let is_tool_loop = aggregate.lane == LANE_TOOL_LOOP;
    let mut data_source_degraded = aggregate.data_source_degraded;
    match aggregate.content {
        GenerationContent::Generated(raw_text) => {
            // Grounded verification leaves prose intact and records unsupported
            // claims as frontmatter-only notes.
            // Curated pages carry no per-file relationship facts; the verifier
            // audits them against members/symbols/source excerpts only.
            let verification_evidence =
                verifier_evidence_rows(members.iter().chain(symbols.iter()));
            let (text, verify_notes) = match verify_with_notes(
                verify,
                &raw_text,
                &verification_evidence,
                &sources,
                &RelationshipFacts::default(),
            ) {
                VerifyOutcome::Skipped => (raw_text.clone(), Vec::new()),
                VerifyOutcome::Verified { text, notes } => (text, notes),
            };
            let grounded = ground_text(&text, spans, None);
            let grounded_empty = grounded.trim().is_empty();
            let has_sections = has_required_curated_sections(kind, &grounded);
            if grounded_empty || !has_sections {
                if is_tool_loop {
                    // The tool loop produced ungroundable or structurally incomplete
                    // prose: invalid, hard-fail (no skeleton fallback) (#978).
                    // The flags/lengths distinguish "grounding stripped every
                    // citation" (spans too narrow) from "missing a required
                    // section" (model output shape).
                    tool_loop_dump::maybe_dump_tool_loop_failure(
                        tool_loop_dump_dir,
                        kind,
                        title,
                        system,
                        &prompt,
                        &raw_text,
                        &text,
                        &grounded,
                    );
                    return Err(anyhow::anyhow!(
                        "Tool-loop curated page '{title}' produced an invalid body \
                         (grounded_empty={grounded_empty}, has_required_sections={has_sections}, \
                         grounded_len={}, generated_len={}); page not written (no skeleton)",
                        grounded.trim().len(),
                        text.len(),
                    ));
                }
                Ok(CuratedBody {
                    body: Some(structural_body(kind, title, &members, &symbols)),
                    degraded_sources: vec!["grounding-empty".to_string()],
                    verify_notes: Vec::new(),
                    observability,
                })
            } else {
                // graph-unavailable (tool-loop evidence degradation) is listed but
                // never marks the page degraded.
                Ok(CuratedBody {
                    body: Some(grounded),
                    degraded_sources: std::mem::take(&mut data_source_degraded),
                    verify_notes,
                    observability,
                })
            }
        }
        // A tool-loop failure already returned `Err` from `generate_aggregate`; this
        // arm is reached only on the one-shot path.
        GenerationContent::Failed(cause) => Ok(CuratedBody {
            body: Some(structural_body(kind, title, &members, &symbols)),
            degraded_sources: vec![cause.reason_code().to_string()],
            verify_notes: Vec::new(),
            observability,
        }),
        GenerationContent::Skipped => Ok(CuratedBody {
            body: Some(structural_body(kind, title, &members, &symbols)),
            degraded_sources: Vec::new(),
            verify_notes: Vec::new(),
            observability,
        }),
    }
}

/// Renders the "Start here — guided tour" block shared by the front page and
/// the concept index: a "new to this codebase" callout, the dependency-ordered
/// narrative chapters numbered 1..N, and a one-line pointer to search the
/// same vault. Navigation only — no new generation. Takes `(slug, title)`
/// pairs so it stays decoupled from the `NarrativePage` struct.
pub(crate) fn append_guided_tour(doc: &mut String, chapters: &[(&str, &str)]) {
    doc.push_str("## Start here — guided tour\n\n");
    if let Some((slug, title)) = chapters.first() {
        let _ = writeln!(
            doc,
            "New to this codebase? Begin with [[code/narrative/{slug}|{title}]].\n"
        );
    }
    for (index, (slug, title)) in chapters.iter().enumerate() {
        let _ = writeln!(doc, "{}. [[code/narrative/{slug}|{title}]]", index + 1);
    }
    doc.push('\n');
    append_search_hint(doc);
}

/// One-line pointer to search over the same vault.
pub(crate) fn append_search_hint(doc: &mut String) {
    doc.push_str("Find pages in this vault with `gwiki search \"...\"`.\n\n");
}

/// Renders the reciprocal `Previous`/`Next` chapter navigation at the foot of a
/// narrative page. The links double as reciprocal wikilinks between adjacent
/// chapters, so the guided tour also strengthens backlinks (#853D).
pub(crate) fn append_tour_nav(
    doc: &mut String,
    prev: Option<(&str, &str)>,
    next: Option<(&str, &str)>,
) {
    if prev.is_none() && next.is_none() {
        return;
    }
    doc.push_str("## Continue the tour\n\n");
    if let Some((slug, title)) = prev {
        let _ = writeln!(doc, "- ← Previous: [[code/narrative/{slug}|{title}]]");
    }
    if let Some((slug, title)) = next {
        let _ = writeln!(doc, "- Next →: [[code/narrative/{slug}|{title}]]");
    }
    doc.push('\n');
}

/// One resolved stage of a curated page's behavior flow.
struct FlowComponent {
    /// Normalized match keys (module name / file stem) used to align this stage
    /// with a documented `A -> B -> C` data-flow chain in the evidence.
    keys: Vec<String>,
    label: String,
    role: Option<String>,
}

/// Flow stages plus the component-id ownership index derived from the same
/// module-first/file-fallback resolution.
struct ResolvedFlowStages {
    components: Vec<FlowComponent>,
    component_owner: BTreeMap<String, usize>,
}

const MAX_CHILD_FLOW_STAGES: usize = 10;
const MAX_CONTAINMENT_NODES: usize = 12;

#[derive(Clone, Debug, PartialEq, Eq)]
struct ChildFlowInputs {
    modules: Vec<String>,
    files: Vec<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct ContainmentNode {
    key: String,
    label: String,
}

/// Bounded source-excerpt budget (chars per file) scanned for a documented
/// data-flow chain. Keeps the hint grounded in real excerpts without pulling
/// whole files into the scan.
const FLOW_HINT_EXCERPT_CHARS: usize = 600;
/// Word cap on a stage's role phrase so node labels stay legible.
const FLOW_ROLE_WORDS: usize = 8;

/// Build the conceptual-behavior flow section for a curated concept/narrative
/// page (#17521): supply the page's evidenced member edges to the LLM
/// composer, then verify and normalize what it draws.
///
/// Evidence, per the diagrams-from-supplied-evidence-only contract:
///
/// * every node is a real member (module/file) with its grounded role phrase,
/// * a documented `A -> B -> C` data-flow chain found in the member summaries
///   or bounded source excerpts contributes its consecutive pairs as edges,
/// * real member-level call/import edges from the code index (graph edges
///   attributed through each member's owning components) contribute the rest.
///
/// A page whose members have no evidenced edges gets no diagram — composing
/// one would fabricate a flow no source evidences. The composer additionally
/// rejects any arrow the model draws that matches no supplied edge and keeps
/// the result island-free. `None` is normal, never degradation. When a member
/// lacks a grounded role the caption carries an honest degradation note.
pub(crate) struct CuratedFlowContext<'a, 'doc> {
    pub(super) page_path: &'a str,
    pub(super) module_lookup: &'a BTreeMap<&'doc str, &'doc ModuleDoc>,
    pub(super) file_lookup: &'a BTreeMap<&'doc str, &'doc FileDoc>,
    pub(super) leading_chunks: &'a BTreeMap<String, LeadingChunk>,
    pub(super) graph_edges: &'a [CodewikiGraphEdge],
    pub(super) diagram_stats: &'a mut DiagramStats,
    pub(super) progress: &'a mut CodewikiProgress,
}

struct CuratedFlowEvidenceContext<'a, 'doc> {
    module_lookup: &'a BTreeMap<&'doc str, &'doc ModuleDoc>,
    file_lookup: &'a BTreeMap<&'doc str, &'doc FileDoc>,
    leading_chunks: &'a BTreeMap<String, LeadingChunk>,
    graph_edges: &'a [CodewikiGraphEdge],
}

pub(crate) fn curated_flow_diagram(
    member_modules: &[String],
    member_files: &[String],
    generate: &mut Option<&mut TextGenerator<'_>>,
    context: CuratedFlowContext<'_, '_>,
) -> Option<String> {
    let CuratedFlowContext {
        page_path,
        module_lookup,
        file_lookup,
        leading_chunks,
        graph_edges,
        diagram_stats,
        progress,
    } = context;
    let mut stages = resolve_flow_stages(member_modules, member_files, module_lookup, file_lookup);
    let evidence_context = CuratedFlowEvidenceContext {
        module_lookup,
        file_lookup,
        leading_chunks,
        graph_edges,
    };
    let mut outcome = compose_curated_flow_attempt(
        &stages,
        member_modules,
        member_files,
        &evidence_context,
        generate,
    );
    let mut pass = "pass 1 member evidence";
    let mut child_rollup = false;

    if matches!(outcome, DiagramOutcome::SparseEvidence) {
        let child_inputs = child_flow_inputs(member_modules, module_lookup, file_lookup);
        stages = resolve_flow_stages(
            &child_inputs.modules,
            &child_inputs.files,
            module_lookup,
            file_lookup,
        );
        outcome = compose_curated_flow_attempt(
            &stages,
            &child_inputs.modules,
            &child_inputs.files,
            &evidence_context,
            generate,
        );
        pass = "pass 2 child evidence";
        child_rollup = true;
    }

    let block = match outcome {
        DiagramOutcome::Emitted(block) => {
            let emitted = DiagramOutcome::Emitted(block.clone());
            diagram_stats.record_named_pass(
                page_path,
                DiagramKind::CuratedFlow,
                &emitted,
                pass,
                progress,
            );
            block
        }
        outcome => {
            diagram_stats.record_named_pass(
                page_path,
                DiagramKind::CuratedFlow,
                &outcome,
                "pass 3 containment fallback",
                progress,
            );
            return containment_structure_section(
                page_path,
                member_modules,
                member_files,
                module_lookup,
                &outcome,
            );
        }
    };

    let degraded = stages
        .components
        .iter()
        .any(|component| component.role.is_none());

    let mut section = String::from("## Conceptual flow\n\n");
    if child_rollup {
        section.push_str(
            "> _Conceptual flow_ — child-level roll-up of this page's module tree, \
composed by the model from supplied evidence only: the data flow documented in \
the child summaries plus child-level call/import edges from the code index. Every \
arrow is verified against that evidence before the diagram is emitted.\n\n",
        );
    } else {
        section.push_str(
            "> _Conceptual flow_ — how this page's subsystems behave together, \
composed by the model from supplied evidence only: the data flow documented in \
the sources plus member-level call/import edges from the code index. Every \
arrow is verified against that evidence before the diagram is emitted.\n\n",
        );
    }
    if degraded {
        section.push_str(
            "> _Degraded:_ one or more subsystems had no indexed summary, so it \
appears by name only.\n\n",
        );
    }
    section.push_str(&block);
    if !section.ends_with('\n') {
        section.push('\n');
    }
    section.push('\n');
    Some(section)
}

fn compose_curated_flow_attempt(
    stages: &ResolvedFlowStages,
    member_modules: &[String],
    member_files: &[String],
    context: &CuratedFlowEvidenceContext<'_, '_>,
    generate: &mut Option<&mut TextGenerator<'_>>,
) -> DiagramOutcome {
    if stages.components.len() < 2 {
        return DiagramOutcome::SparseEvidence;
    }
    let hint = flow_hint_text(
        member_modules,
        member_files,
        context.module_lookup,
        context.file_lookup,
        context.leading_chunks,
    );
    let evidence = curated_flow_evidence(
        &stages.components,
        &hint,
        &stages.component_owner,
        context.graph_edges,
    );
    compose_flowchart(
        generate,
        &evidence,
        "how this page's subsystems behave together",
    )
}

fn child_flow_inputs(
    member_modules: &[String],
    module_lookup: &BTreeMap<&str, &ModuleDoc>,
    file_lookup: &BTreeMap<&str, &FileDoc>,
) -> ChildFlowInputs {
    let mut module_scores: BTreeMap<String, usize> = BTreeMap::new();
    let mut direct_files: BTreeSet<String> = BTreeSet::new();
    for member in member_modules.iter().collect::<BTreeSet<_>>() {
        let Some(doc) = module_lookup.get(member.as_str()) else {
            continue;
        };
        for child in &doc.child_modules {
            let Some(child_doc) = module_lookup.get(child.module.as_str()) else {
                continue;
            };
            module_scores
                .entry(child.module.clone())
                .and_modify(|score| *score = (*score).max(child_doc.direct_files.len()))
                .or_insert(child_doc.direct_files.len());
        }
        direct_files.extend(
            doc.direct_files
                .iter()
                .filter(|file| file_lookup.contains_key(file.path.as_str()))
                .map(|file| file.path.clone()),
        );
    }

    let mut ranked_modules: Vec<(String, usize)> = module_scores.into_iter().collect();
    ranked_modules.sort_by(|(left_name, left_files), (right_name, right_files)| {
        right_files
            .cmp(left_files)
            .then_with(|| left_name.cmp(right_name))
    });
    let modules: Vec<String> = ranked_modules
        .into_iter()
        .take(MAX_CHILD_FLOW_STAGES)
        .map(|(module, _)| module)
        .collect();
    let files = if modules.len() < 2 {
        direct_files
            .into_iter()
            .take(MAX_CHILD_FLOW_STAGES.saturating_sub(modules.len()))
            .collect()
    } else {
        Vec::new()
    };
    ChildFlowInputs { modules, files }
}

fn containment_structure_section(
    page_path: &str,
    member_modules: &[String],
    member_files: &[String],
    module_lookup: &BTreeMap<&str, &ModuleDoc>,
    trigger: &DiagramOutcome,
) -> Option<String> {
    let reason = match trigger {
        DiagramOutcome::SparseEvidence => {
            "no cross-member call/import edges were found in the index."
        }
        DiagramOutcome::NoGenerator => "no diagram generator was available.",
        DiagramOutcome::Rejected => {
            "the generated flow diagram failed verification and was discarded."
        }
        DiagramOutcome::Emitted(_) => return None,
    };

    let mut members: BTreeMap<String, String> = BTreeMap::new();
    let mut children: BTreeMap<String, BTreeMap<String, String>> = BTreeMap::new();
    for module in member_modules {
        let key = format!("module:{module}");
        members.insert(key.clone(), module.clone());
        if let Some(doc) = module_lookup.get(module.as_str()) {
            let child_nodes = children.entry(key).or_default();
            for child in &doc.child_modules {
                child_nodes.insert(format!("module:{}", child.module), child.module.clone());
            }
            for file in &doc.direct_files {
                child_nodes.insert(format!("file:{}", file.path), file.path.clone());
            }
        }
    }
    for file in member_files {
        members.insert(format!("file:{file}"), file.clone());
    }

    let mut nodes = vec![ContainmentNode {
        key: "page".to_string(),
        label: page_path.to_string(),
    }];
    let mut edges: BTreeSet<(String, String)> = BTreeSet::new();
    let mut selected_members = Vec::new();
    for (key, label) in members {
        if nodes.len() == MAX_CONTAINMENT_NODES {
            break;
        }
        edges.insert(("page".to_string(), key.clone()));
        selected_members.push(key.clone());
        nodes.push(ContainmentNode { key, label });
    }
    let mut selected_keys: BTreeSet<String> = nodes.iter().map(|node| node.key.clone()).collect();
    for member in selected_members {
        let Some(member_children) = children.get(&member) else {
            continue;
        };
        for (key, label) in member_children {
            if selected_keys.contains(key) {
                edges.insert((member.clone(), key.clone()));
                continue;
            }
            if nodes.len() == MAX_CONTAINMENT_NODES {
                break;
            }
            selected_keys.insert(key.clone());
            edges.insert((member.clone(), key.clone()));
            nodes.push(ContainmentNode {
                key: key.clone(),
                label: label.clone(),
            });
        }
        if nodes.len() == MAX_CONTAINMENT_NODES {
            break;
        }
    }

    let node_ids: BTreeMap<&str, String> = nodes
        .iter()
        .enumerate()
        .map(|(index, node)| (node.key.as_str(), format!("n{index}")))
        .collect();
    let mut block = String::from("```mermaid\nflowchart TD\n");
    for node in &nodes {
        let id = node_ids.get(node.key.as_str())?;
        let _ = writeln!(block, "    {id}[\"{}\"]", mermaid_label(&node.label));
    }
    for (parent, child) in edges {
        let (Some(parent_id), Some(child_id)) =
            (node_ids.get(parent.as_str()), node_ids.get(child.as_str()))
        else {
            continue;
        };
        let _ = writeln!(block, "    {parent_id} --> {child_id}");
    }
    block.push_str("```\n");
    if !is_valid_mermaid(&block) {
        return None;
    }

    let mut section = String::from("## Conceptual flow\n\n");
    let _ = writeln!(
        section,
        "> Structure map — containment from the module tree; {reason} This shows structure, not runtime flow.\n"
    );
    section.push_str(&block);
    section.push('\n');
    Some(section)
}

/// Reduce a curated page's members to the evidence graph the composer may draw
/// from. Nodes are the resolved flow components (`s0..sN`, labelled with the
/// member name and its grounded role phrase). Edges come from two evidenced
/// sources only: the documented data-flow chain in `hint` (consecutive pairs,
/// in documented order) and member-level call/import graph edges attributed
/// through each member's owning components.
#[allow(clippy::too_many_arguments)]
fn curated_flow_evidence(
    components: &[FlowComponent],
    hint: &str,
    component_owner: &BTreeMap<String, usize>,
    graph_edges: &[CodewikiGraphEdge],
) -> DiagramEvidence {
    let mut evidence = DiagramEvidence::default();
    for (index, component) in components.iter().enumerate() {
        let label = match component.role.as_deref() {
            Some(role) if !role.is_empty() => format!("{} — {role}", component.label),
            _ => component.label.clone(),
        };
        evidence.push_node(format!("s{index}"), label, NodeShape::Box);
    }

    // Documented data-flow chain: an `A -> B -> C` arrow chain in the member
    // summaries or bounded source excerpts evidences its consecutive pairs.
    let chain = parse_flow_chain(hint, components);
    let mut stage_edges: BTreeSet<(usize, usize)> = BTreeSet::new();
    for pair in chain.windows(2) {
        push_stage_edge_once(&mut evidence, &mut stage_edges, pair[0], pair[1], None);
    }

    // Member-level call/import edges from the code index: attribute each
    // graph edge through the components' owning members and keep only
    // cross-member edges. Which member owns a component follows the same
    // resolution as `resolve_flow_stages`: modules own their files' components
    // (including descendant modules); explicit member files own their own.
    for edge in graph_edges {
        let Some(source) = component_owner.get(edge.source_component_id.as_str()) else {
            continue;
        };
        let Some(target) = component_owner.get(edge.target_component_id.as_str()) else {
            continue;
        };
        if source == target {
            continue;
        }
        let label = match edge.kind {
            CodewikiGraphEdgeKind::Call => "calls",
            CodewikiGraphEdgeKind::Import => "imports",
        };
        push_stage_edge_once(
            &mut evidence,
            &mut stage_edges,
            *source,
            *target,
            Some(label.to_string()),
        );
    }

    evidence
}

fn push_stage_edge_once(
    evidence: &mut DiagramEvidence,
    stage_edges: &mut BTreeSet<(usize, usize)>,
    source: usize,
    target: usize,
    label: Option<String>,
) {
    if !stage_edges.insert((source, target)) {
        return;
    }
    evidence.push_edge(format!("s{source}"), format!("s{target}"), label, false);
}

/// Resolve the page's members into flow stages. Modules are the subsystem unit;
/// files only flesh out the flow when there are too few modules to chain on
/// their own. The returned ownership map is derived from the same ordered stage
/// list so flow stage ids and component ownership cannot drift.
fn resolve_flow_stages(
    member_modules: &[String],
    member_files: &[String],
    module_lookup: &BTreeMap<&str, &ModuleDoc>,
    file_lookup: &BTreeMap<&str, &FileDoc>,
) -> ResolvedFlowStages {
    let mut components: Vec<FlowComponent> = Vec::new();
    let mut component_owner: BTreeMap<String, usize> = BTreeMap::new();
    for module in member_modules {
        if let Some(doc) = module_lookup.get(module.as_str()) {
            let stage = components.len();
            components.push(component_from(&doc.module, &doc.summary));
            for file in file_lookup.values() {
                if file.module == *module || module_is_ancestor(module, &file.module) {
                    for component in &file.component_ids {
                        component_owner.entry(component.clone()).or_insert(stage);
                    }
                }
            }
        }
    }
    if components.len() < 2 {
        for file in member_files {
            if let Some(doc) = file_lookup.get(file.as_str()) {
                let stage = components.len();
                components.push(component_from(&doc.path, &doc.summary));
                for component in &doc.component_ids {
                    component_owner.insert(component.clone(), stage);
                }
            }
        }
    }
    ResolvedFlowStages {
        components,
        component_owner,
    }
}

fn component_from(name: &str, summary: &str) -> FlowComponent {
    let label = flow_label(name);
    let mut keys = vec![normalize_key(name)];
    let label_key = normalize_key(&label);
    if !label_key.is_empty() && !keys.contains(&label_key) {
        keys.push(label_key);
    }
    keys.retain(|key| !key.is_empty());
    FlowComponent {
        keys,
        label,
        role: role_phrase(summary),
    }
}

/// Concatenate the member summaries and bounded leading-chunk excerpts into one
/// scan buffer for documented-data-flow detection.
fn flow_hint_text(
    member_modules: &[String],
    member_files: &[String],
    module_lookup: &BTreeMap<&str, &ModuleDoc>,
    file_lookup: &BTreeMap<&str, &FileDoc>,
    leading_chunks: &BTreeMap<String, LeadingChunk>,
) -> String {
    let mut text = String::new();
    for module in member_modules {
        if let Some(doc) = module_lookup.get(module.as_str()) {
            text.push_str(&doc.summary);
            text.push('\n');
        }
    }
    for file in member_files {
        if let Some(doc) = file_lookup.get(file.as_str()) {
            text.push_str(&doc.summary);
            text.push('\n');
        }
        if let Some(excerpt) = source_excerpt_for_file(file, leading_chunks) {
            let head: String = excerpt
                .excerpt
                .chars()
                .take(FLOW_HINT_EXCERPT_CHARS)
                .collect();
            text.push_str(&head);
            text.push('\n');
        }
    }
    text
}

/// Find the first arrow-delimited line in `hint` that maps at least two
/// components, returning their indices in documented order. Recognises ASCII
/// `->`/`-->` and the Unicode `→`. The consecutive pairs of this chain are
/// documented data-flow evidence edges; a hint with no such chain contributes
/// none.
fn parse_flow_chain(hint: &str, components: &[FlowComponent]) -> Vec<usize> {
    let normalized = hint.replace("-->", "\u{2192}").replace("->", "\u{2192}");
    for line in normalized.lines() {
        if !line.contains('\u{2192}') {
            continue;
        }
        let mut chain: Vec<usize> = Vec::new();
        for segment in line.split('\u{2192}') {
            if let Some(index) = first_component_in(segment, components) {
                push_unique(&mut chain, index);
            }
        }
        if chain.len() >= 2 {
            return chain;
        }
    }
    Vec::new()
}

/// Index of the first component named by any word in `segment`.
fn first_component_in(segment: &str, components: &[FlowComponent]) -> Option<usize> {
    segment
        .split(|c: char| !c.is_ascii_alphanumeric() && c != '_')
        .map(normalize_key)
        .filter(|key| !key.is_empty())
        .find_map(|key| {
            components
                .iter()
                .position(|component| component.keys.contains(&key))
        })
}

fn push_unique(chain: &mut Vec<usize>, index: usize) {
    if !chain.contains(&index) {
        chain.push(index);
    }
}

/// Short, stable node label for a module name or file path: the last path/`::`
/// segment, with a `.rs` extension trimmed.
fn flow_label(name: &str) -> String {
    name.rsplit(['/', ':'])
        .next()
        .unwrap_or(name)
        .trim_end_matches(".rs")
        .trim()
        .to_string()
}

/// Lowercase alphanumeric match key from the last path/`::` segment (extension
/// dropped), for aligning members with a documented data-flow chain.
fn normalize_key(text: &str) -> String {
    let last = text.rsplit(['/', ':']).next().unwrap_or(text);
    let stem = last.split('.').next().unwrap_or(last);
    stem.chars()
        .filter(|c| c.is_ascii_alphanumeric())
        .flat_map(char::to_lowercase)
        .collect()
}

/// First clause of a member summary as the stage's behavior role. The clause is
/// cut at sentence punctuation; when it would blow the [`FLOW_ROLE_WORDS`] cap,
/// it is clipped back to the longest comma-bounded prefix that fits, so the
/// label stays a complete thought. `None` for an empty summary or when no
/// boundary fits the cap — a mid-thought fragment is worse than showing the
/// stage by name only and marking the flow degraded.
fn role_phrase(summary: &str) -> Option<String> {
    let summary = summary.trim();
    if summary.is_empty() {
        return None;
    }
    let clause = summary
        .split_terminator(['.', ';', ':'])
        .next()
        .unwrap_or(summary)
        .trim();
    let within_cap = |text: &str| text.split_whitespace().count() <= FLOW_ROLE_WORDS;
    if within_cap(clause) {
        return normalized_phrase(clause);
    }
    let mut best: Option<&str> = None;
    for (offset, _) in clause.match_indices(',') {
        let candidate = clause[..offset].trim_end();
        if !within_cap(candidate) {
            // Prefixes only grow; nothing later can fit either.
            break;
        }
        best = Some(candidate);
    }
    best.and_then(normalized_phrase)
}

/// Whitespace-collapsed copy of `text`, or `None` when it holds no words.
fn normalized_phrase(text: &str) -> Option<String> {
    let phrase = text.split_whitespace().collect::<Vec<_>>().join(" ");
    (!phrase.is_empty()).then_some(phrase)
}
