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

use std::collections::BTreeMap;
use std::fmt::Write as _;

use super::super::*;

#[path = "curated_content/lane_b_dump.rs"]
mod lane_b_dump;
#[cfg(test)]
#[path = "curated_content/tests.rs"]
mod tests;

/// Cap on key-symbol evidence rows fed into one content prompt. Bounds prompt
/// size; the structural fallback table reuses the same cap.
const MAX_PAGE_SYMBOL_ROWS: usize = 12;
const MAX_STRUCTURAL_KEY_COMPONENTS: usize = 6;

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
    /// Per-page Lane B tool-loop observability, recorded into the page's
    /// frontmatter when the run used the tool loop (#978). Default (zero counts)
    /// for the Lane A / structural path.
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
    generate: &mut Option<&mut TextGenerator<'_>>,
    tool_loop: &mut Option<&mut ToolLoopGenerator<'_>>,
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

    // Aggregate-tier page: Lane B tool loop when configured, else Lane A. A
    // Lane B generation failure hard-fails the run via `generate_aggregate`; an
    // empty/incomplete Lane B body is also a hard fail below (no skeleton). Lane
    // A failures degrade to the structural body, as before.
    let aggregate = generate_aggregate(
        tool_loop,
        generate,
        &prompt,
        system,
        &format!("curated page '{title}'"),
    )?;
    let observability = aggregate.observability.clone();
    let is_lane_b = aggregate.lane == LANE_TOOL_LOOP;
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
                if is_lane_b {
                    // Lane B produced ungroundable or structurally incomplete
                    // prose: invalid, hard-fail (no skeleton fallback) (#978).
                    // The flags/lengths distinguish "grounding stripped every
                    // citation" (spans too narrow) from "missing a required
                    // section" (model output shape).
                    lane_b_dump::maybe_dump_lane_b_failure(
                        kind, title, system, &prompt, &raw_text, &text, &grounded,
                    );
                    return Err(anyhow::anyhow!(
                        "Lane B curated page '{title}' produced an invalid body \
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
                // graph-unavailable (Lane B evidence degradation) is listed but
                // never marks the page degraded.
                Ok(CuratedBody {
                    body: Some(grounded),
                    degraded_sources: std::mem::take(&mut data_source_degraded),
                    verify_notes,
                    observability,
                })
            }
        }
        // A Lane B failure already returned `Err` from `generate_aggregate`; this
        // arm is reached only on the Lane A one-shot path.
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

fn member_evidence_rows(
    member_modules: &[String],
    member_files: &[String],
    module_lookup: &BTreeMap<&str, &ModuleDoc>,
    file_lookup: &BTreeMap<&str, &FileDoc>,
) -> Vec<prompts::PageEvidenceRow> {
    let mut rows = Vec::new();
    for module in member_modules {
        if let Some(doc) = module_lookup.get(module.as_str()) {
            rows.push(prompts::PageEvidenceRow {
                name: doc.module.clone(),
                kind: "module".to_string(),
                citation: span_citation(&doc.source_spans, &doc.module),
                summary: doc.summary.clone(),
            });
        }
    }
    for file in member_files {
        if let Some(doc) = file_lookup.get(file.as_str()) {
            rows.push(prompts::PageEvidenceRow {
                name: doc.path.clone(),
                kind: "file".to_string(),
                citation: span_citation(&doc.source_spans, &doc.path),
                summary: doc.summary.clone(),
            });
        }
    }
    rows
}

fn symbol_evidence_rows(
    member_files: &[String],
    file_lookup: &BTreeMap<&str, &FileDoc>,
) -> Vec<prompts::PageEvidenceRow> {
    let mut rows = Vec::new();
    for file in member_files {
        if let Some(doc) = file_lookup.get(file.as_str()) {
            for symbol in &doc.symbols {
                rows.push(prompts::PageEvidenceRow {
                    name: symbol.symbol.name.clone(),
                    kind: symbol.symbol.kind.clone(),
                    citation: symbol.source_span.citation(),
                    summary: symbol.purpose.clone(),
                });
            }
        }
    }
    // Most important first (most defined symbols → most central), stable on ties.
    rows.sort_by(|a, b| a.name.cmp(&b.name));
    rows.truncate(MAX_PAGE_SYMBOL_ROWS);
    rows
}

fn verifier_evidence_rows<'a>(
    rows: impl Iterator<Item = &'a prompts::PageEvidenceRow>,
) -> Vec<prompts::SymbolSummary> {
    rows.enumerate()
        .map(|(index, row)| {
            let (line_start, line_end) = citation_line_range(&row.citation).unwrap_or((1, 1));
            prompts::SymbolSummary {
                name: row.name.clone(),
                kind: row.kind.clone(),
                component_id: format!("curated-evidence-{}", index + 1),
                component_label: row.citation.clone(),
                line_start,
                line_end,
                purpose: row.summary.clone(),
            }
        })
        .collect()
}

fn citation_line_range(citation: &str) -> Option<(usize, usize)> {
    let inner = citation.strip_prefix('[')?.strip_suffix(']')?;
    let (_file, range) = inner.rsplit_once(':')?;
    let (start, end) = match range.split_once('-') {
        Some((start, end)) => (start.parse().ok()?, end.parse().ok()?),
        None => {
            let line = range.parse().ok()?;
            (line, line)
        }
    };
    Some((start, end))
}

fn span_citation(spans: &[SourceSpan], fallback: &str) -> String {
    spans
        .first()
        .map(SourceSpan::citation)
        .unwrap_or_else(|| fallback.to_string())
}

/// Deterministic multi-section fallback body: a real `## Purpose`, a
/// `## Key components` table grounded in symbol citations, and a member list.
/// Mirrors `structural_file_summary` so `--ai off` and content-pass failures
/// still yield structure, not a bare summary.
fn structural_body(
    kind: CuratedPageKind,
    title: &str,
    members: &[prompts::PageEvidenceRow],
    symbols: &[prompts::PageEvidenceRow],
) -> String {
    let mut body = String::new();
    match kind {
        CuratedPageKind::Concept => {
            write_section(
                &mut body,
                "Purpose",
                &format!(
                    "{title} is a source-backed concept assembled from the related \
                     modules and files below. Use this page as a handbook entry \
                     point, then drill into the linked reference pages for \
                     implementation detail."
                ),
            );
            write_section(
                &mut body,
                "How it works",
                &component_walkthrough(title, members, symbols),
            );
        }
        CuratedPageKind::Narrative => {
            write_section(
                &mut body,
                "Why this matters",
                &format!(
                    "{title} is part of the guided tour through the source-backed \
                     reference. It explains why this area matters before sending \
                     the reader into the exact modules, files, and symbols."
                ),
            );
            write_section(
                &mut body,
                "How it works",
                &component_walkthrough(title, members, symbols),
            );
        }
    }

    append_structural_key_components(&mut body, members, symbols);
    append_structural_failure_modes(&mut body, members, symbols);
    append_structural_change_guide(&mut body, members, symbols);
    append_structural_next_steps(&mut body, members, symbols);
    body
}

fn append_structural_key_components(
    body: &mut String,
    members: &[prompts::PageEvidenceRow],
    symbols: &[prompts::PageEvidenceRow],
) {
    body.push_str("## Key components\n\n");
    body.push_str(
        "These are the highest-signal grounded entries for this page. The full \
         reference remains in the linked module and file pages.\n\n",
    );
    if !symbols.is_empty() {
        write_markdown_table_header(body, &["Symbol", "Kind", "Source", "Role"]);
        for row in symbols.iter().take(MAX_STRUCTURAL_KEY_COMPONENTS) {
            write_markdown_table_row(
                body,
                [
                    row.name.clone(),
                    row.kind.clone(),
                    row.citation.clone(),
                    row.summary.clone(),
                ],
            );
        }
    } else if !members.is_empty() {
        write_markdown_table_header(body, &["Member", "Kind", "Source", "Role"]);
        for row in members.iter().take(MAX_STRUCTURAL_KEY_COMPONENTS) {
            write_markdown_table_row(
                body,
                [
                    row.name.clone(),
                    row.kind.clone(),
                    row.citation.clone(),
                    row.summary.clone(),
                ],
            );
        }
    } else {
        body.push_str("- No indexed components were available for this page.\n");
    }
    body.push('\n');
}

fn append_structural_failure_modes(
    body: &mut String,
    members: &[prompts::PageEvidenceRow],
    symbols: &[prompts::PageEvidenceRow],
) {
    body.push_str("## Failure modes\n\n");
    body.push_str(
        "This structural section is conservative: it names only failure signals \
         that can be inferred from the available source-backed evidence.\n\n",
    );
    write_markdown_table_header(body, &["Signal", "What to inspect", "Evidence"]);
    let evidence = first_citation(members, symbols);
    write_markdown_table_row(
        body,
        [
            "Generated prose unavailable".to_string(),
            "The page fell back to deterministic structure; regenerate with an AI aggregate pass and inspect verify_notes.".to_string(),
            evidence.clone(),
        ],
    );
    write_markdown_table_row(
        body,
        [
            "Behavior unclear".to_string(),
            "Open the linked module or file page before changing code that is only summarized here.".to_string(),
            evidence,
        ],
    );
    body.push('\n');
}

fn append_structural_change_guide(
    body: &mut String,
    members: &[prompts::PageEvidenceRow],
    symbols: &[prompts::PageEvidenceRow],
) {
    body.push_str("## How to change it\n\n");
    body.push_str(
        "Start from the grounded entries below, make the code change in the \
         linked module or file, then regenerate the codewiki so citations and \
         verify_notes reflect the new source.\n\n",
    );
    for row in members
        .iter()
        .chain(symbols.iter())
        .take(MAX_STRUCTURAL_KEY_COMPONENTS)
    {
        let _ = writeln!(
            body,
            "- Inspect `{}` ({}) at {} before editing.",
            row.name, row.kind, row.citation
        );
    }
    if members.is_empty() && symbols.is_empty() {
        body.push_str("- Add module or file evidence before making a behavioral claim here.\n");
    }
    body.push('\n');
}

fn append_structural_next_steps(
    body: &mut String,
    members: &[prompts::PageEvidenceRow],
    symbols: &[prompts::PageEvidenceRow],
) {
    body.push_str("## What to read next\n\n");
    for row in members
        .iter()
        .chain(symbols.iter())
        .take(MAX_STRUCTURAL_KEY_COMPONENTS)
    {
        let _ = writeln!(body, "- `{}` ({}) - {}", row.name, row.kind, row.citation);
    }
    if members.is_empty() && symbols.is_empty() {
        body.push_str("- Return to the concept tree and choose a page with source members.\n");
    }
    body.push('\n');
}

fn component_walkthrough(
    title: &str,
    members: &[prompts::PageEvidenceRow],
    symbols: &[prompts::PageEvidenceRow],
) -> String {
    let mut body = format!(
        "The {title} page is grounded by a bounded set of modules, files, and \
         symbols rather than an exhaustive dump.\n\n"
    );
    for (index, row) in members
        .iter()
        .chain(symbols.iter())
        .take(MAX_STRUCTURAL_KEY_COMPONENTS)
        .enumerate()
    {
        let _ = writeln!(
            body,
            "{}. `{}` ({}) anchors the walkthrough at {}.",
            index + 1,
            row.name,
            row.kind,
            row.citation
        );
    }
    if members.is_empty() && symbols.is_empty() {
        body.push_str("No grounded members were available for a step-by-step walkthrough.");
    }
    body
}

fn has_required_curated_sections(kind: CuratedPageKind, body: &str) -> bool {
    let required: &[&str] = match kind {
        CuratedPageKind::Concept => &[
            "## Purpose",
            "## How it works",
            "## Key components",
            "## Failure modes",
            "## How to change it",
            "## What to read next",
        ],
        CuratedPageKind::Narrative => &[
            "## Why this matters",
            "## How it works",
            "## Key components",
            "## Failure modes",
            "## How to change it",
            "## What to read next",
        ],
    };
    let h2_titles = body
        .lines()
        .filter_map(|line| line.strip_prefix("## "))
        .filter(|title| !title.starts_with('#'))
        .map(str::trim)
        .map(|title| title.trim_end_matches('#').trim())
        .collect::<std::collections::BTreeSet<_>>();
    required
        .iter()
        .map(|heading| heading.trim_start_matches("## "))
        .all(|title| h2_titles.contains(title))
}

fn first_citation(
    members: &[prompts::PageEvidenceRow],
    symbols: &[prompts::PageEvidenceRow],
) -> String {
    members
        .iter()
        .chain(symbols.iter())
        .next()
        .map(|row| row.citation.clone())
        .unwrap_or_else(|| "No source member available".to_string())
}

/// Renders the "Start here — guided tour" block shared by the front page and
/// the concept index: a "new to this codebase" callout, the dependency-ordered
/// narrative chapters numbered 1..N, and a one-line pointer to ask/search the
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
    append_ask_hint(doc);
}

/// One-line pointer to the conversational/search leg over the same vault.
pub(crate) fn append_ask_hint(doc: &mut String) {
    doc.push_str(
        "Ask questions across this vault with `gwiki ask \"...\"`, or find pages with `gwiki search \"...\"`.\n\n",
    );
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

/// Bounded source-excerpt budget (chars per file) scanned for a documented
/// data-flow chain. Keeps the hint grounded in real excerpts without pulling
/// whole files into the scan.
const FLOW_HINT_EXCERPT_CHARS: usize = 600;
/// Word cap on a stage's role phrase so node labels stay legible.
const FLOW_ROLE_WORDS: usize = 8;

/// Build the conceptual-behavior flow diagram for a curated concept/narrative
/// page. Grounded strictly in the page's member modules/files: each stage is a
/// real member, its role phrase comes from that member's grounded summary, and
/// the stage order follows a documented data flow — an `A -> B -> C` arrow chain
/// found in the member summaries or bounded source excerpts (e.g. a CLAUDE.md
/// "Data Flow" section indexed into the evidence) — when present, otherwise the
/// member declaration order. Returns `None` when there are fewer than two
/// members to chain, so callers can simply omit the section. When a stage lacks
/// a grounded role the diagram shows it by name only and its caption carries an
/// honest degradation note. Works for repos without curated docs because
/// module/file summaries are always available from indexing.
pub(crate) fn curated_flow_diagram(
    member_modules: &[String],
    member_files: &[String],
    module_lookup: &BTreeMap<&str, &ModuleDoc>,
    file_lookup: &BTreeMap<&str, &FileDoc>,
    leading_chunks: &BTreeMap<String, LeadingChunk>,
) -> Option<String> {
    let mut components = flow_components(member_modules, member_files, module_lookup, file_lookup);
    if components.len() < 2 {
        return None;
    }

    let hint = flow_hint_text(
        member_modules,
        member_files,
        module_lookup,
        file_lookup,
        leading_chunks,
    );
    let ordered_from_docs = order_components_by_hint(&mut components, &hint);
    let degraded = components.iter().any(|component| component.role.is_none());

    let steps = components
        .iter()
        .enumerate()
        .map(
            |(index, component)| architecture_diagrams::ConceptualFlowStep {
                id: format!("s{index}"),
                label: component.label.clone(),
                role: component.role.clone(),
            },
        )
        .collect::<Vec<_>>();

    architecture_diagrams::render_conceptual_flow(&steps, ordered_from_docs, degraded)
}

/// Resolve the page's members into flow stages. Modules are the subsystem unit;
/// files only flesh out the flow when there are too few modules to chain on
/// their own.
fn flow_components(
    member_modules: &[String],
    member_files: &[String],
    module_lookup: &BTreeMap<&str, &ModuleDoc>,
    file_lookup: &BTreeMap<&str, &FileDoc>,
) -> Vec<FlowComponent> {
    let mut components: Vec<FlowComponent> = Vec::new();
    for module in member_modules {
        if let Some(doc) = module_lookup.get(module.as_str()) {
            components.push(component_from(&doc.module, &doc.summary));
        }
    }
    if components.len() < 2 {
        for file in member_files {
            if let Some(doc) = file_lookup.get(file.as_str()) {
                components.push(component_from(&doc.path, &doc.summary));
            }
        }
    }
    components
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

/// Reorder `components` to follow a documented `A -> B -> C` data-flow chain in
/// `hint` when one references at least two of them. Returns true when the order
/// came from such a chain. Recognises ASCII `->`/`-->` and the Unicode `→`.
fn order_components_by_hint(components: &mut Vec<FlowComponent>, hint: &str) -> bool {
    let chain = parse_flow_chain(hint, components);
    if chain.len() < 2 {
        return false;
    }
    // Chain-matched stages first (documented order), then any remaining members
    // in their original order. Drain into slots so each stage moves exactly once.
    let mut slots: Vec<Option<FlowComponent>> = components.drain(..).map(Some).collect();
    let mut ordered: Vec<FlowComponent> = Vec::with_capacity(slots.len());
    for &index in &chain {
        if let Some(component) = slots[index].take() {
            ordered.push(component);
        }
    }
    for slot in &mut slots {
        if let Some(component) = slot.take() {
            ordered.push(component);
        }
    }
    *components = ordered;
    true
}

/// Find the first arrow-delimited line in `hint` that maps at least two
/// components, returning their indices in documented order.
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

/// First clause of a member summary, capped to a few words, as the stage's
/// behavior role. `None` for an empty summary so the renderer shows the stage by
/// name only and marks the flow degraded.
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
    let phrase = clause
        .split_whitespace()
        .take(FLOW_ROLE_WORDS)
        .collect::<Vec<_>>()
        .join(" ");
    (!phrase.is_empty()).then_some(phrase)
}
