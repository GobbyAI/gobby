//! Grounded evidence preparation and deterministic fallback bodies for curated pages.

use super::*;

/// Cap on key-symbol evidence rows fed into one content prompt. Bounds prompt
/// size; the structural fallback table reuses the same cap.
const MAX_PAGE_SYMBOL_ROWS: usize = 12;
const MAX_STRUCTURAL_KEY_COMPONENTS: usize = 6;

pub(super) fn member_evidence_rows(
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

pub(super) fn symbol_evidence_rows(
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
    // Retain the alphabetically first rows for deterministic bounded prompts.
    rows.sort_by(|a, b| a.name.cmp(&b.name));
    rows.truncate(MAX_PAGE_SYMBOL_ROWS);
    rows
}

pub(super) fn verifier_evidence_rows<'a>(
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
pub(super) fn structural_body(
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

pub(super) fn has_required_curated_sections(kind: CuratedPageKind, body: &str) -> bool {
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
