use std::fmt::Write as _;

use super::super::*;

/// Render the deterministic deprecations aggregate page (#889). Lists every
/// deprecated symbol grouped by file; renders a clear "none found" line when
/// empty. Never degrades.
pub(crate) fn render_deprecations_doc(doc: &DeprecationsDoc) -> String {
    let mut out = frontmatter_with_degradation_without_ranges(
        "Deprecations",
        "code_deprecations",
        &[],
        &doc.degraded_sources,
    );
    out.push_str("# Deprecations\n\n");
    out.push_str(
        "This page is derived deterministically from a source scan of the documented files \
         — no LLM. A symbol is listed when a `#[deprecated]` attribute or a `DEPRECATED` \
         doc-comment sits directly above its definition (or in its docstring). Each row is the \
         symbol that carries the marker, where it lives, and the recorded reason.\n\n",
    );

    if doc.symbols.is_empty() {
        out.push_str("No deprecated symbols found.\n");
        return out;
    }

    // Group by file in the already-sorted order.
    let mut current_file: Option<&str> = None;
    for symbol in &doc.symbols {
        if current_file != Some(symbol.file.as_str()) {
            if current_file.is_some() {
                out.push('\n');
            }
            let _ = writeln!(out, "## {}\n", file_wikilink(&symbol.file));
            write_markdown_table_header(&mut out, &["Symbol", "Kind", "Location", "Reason"]);
            current_file = Some(symbol.file.as_str());
        }
        let location = format!("{}:{}", symbol.file, symbol.line);
        let reason = if symbol.reason.is_empty() {
            "deprecated".to_string()
        } else {
            symbol.reason.clone()
        };
        write_markdown_table_row(
            &mut out,
            [
                inline_code(&symbol.name),
                symbol.kind.clone(),
                inline_code(&location),
                reason,
            ],
        );
    }
    out.push('\n');
    out
}

/// Render the deterministic dead-code-candidates page (#889). Leads with a
/// prominent "CANDIDATES, not verdicts" disclaimer and the exact heuristic,
/// then lists candidates grouped by file. NEVER degrades: an unavailable graph
/// renders only a skip note.
pub(crate) fn render_dead_code_doc(doc: &DeadCodeDoc) -> String {
    let mut out = frontmatter_with_degradation_without_ranges(
        "Dead-Code Candidates",
        "code_dead_code_candidates",
        &[],
        &doc.degraded_sources,
    );
    out.push_str("# Dead-Code Candidates\n\n");
    out.push_str(
        "> **CANDIDATES, not verdicts.** Everything below is a heuristic suggestion derived \
         deterministically from the code graph and a source scan — no LLM. A symbol appearing \
         here is *not* proof it is dead: reflection, trait objects, macros, FFI, conditional \
         compilation, and external/test callers can all reach code without a recorded Call \
         edge. Treat each entry as a lead to investigate, never as a delete list.\n\n",
    );

    if doc.skipped {
        out.push_str(
            "Dead-code analysis needs the code graph, which was unavailable for this run. \
             No candidates were computed.\n",
        );
        return out;
    }

    out.push_str("## Heuristic\n\n");
    out.push_str(
        "A symbol is listed as a candidate only when ALL of the following hold:\n\n\
         1. It is a real definition (function, struct, enum, trait, class, type, or constant) — \
         import/use rows and other index noise are skipped.\n\
         2. It has zero inbound calls: no Call edge in the code graph targets it. (Import edges \
         are file-level, not symbol-level, so they never count as a call.)\n\
         3. It is not an entry point: its name is not `main`, and its `(file, name)` is not one \
         of the CLI contract handler entry points (those are reached from dispatch, not via a \
         Call edge).\n\
         4. It is not test-gated: no `#[test]`/`#[cfg(test)]` attribute sits above it, and it \
         does not live under a tests path.\n\
         5. It is not a trait impl or method: its signature does not start with `impl `/`unsafe \
         impl `, and its kind is not `method`. Methods are dispatched (often via traits or \
         dynamic dispatch), so direct Call edges do not reliably represent them — they are \
         excluded to avoid false positives.\n\n",
    );

    if doc.truncated {
        out.push_str(
            "> Note: the code graph was truncated for this run, so some inbound calls may be \
             missing. This list may be incomplete and may contain extra entries.\n\n",
        );
    }

    if doc.capped {
        let _ = writeln!(
            out,
            "> Note: the candidate list was capped at {} entries; additional candidates were \
             omitted.\n",
            doc.candidates.len()
        );
    }

    if doc.candidates.is_empty() {
        out.push_str("No dead-code candidates found.\n");
        return out;
    }

    let mut current_file: Option<&str> = None;
    for candidate in &doc.candidates {
        if current_file != Some(candidate.file.as_str()) {
            if current_file.is_some() {
                out.push('\n');
            }
            let _ = writeln!(out, "## {}\n", file_wikilink(&candidate.file));
            write_markdown_table_header(&mut out, &["Symbol", "Kind", "Location"]);
            current_file = Some(candidate.file.as_str());
        }
        let location = format!("{}:{}", candidate.file, candidate.line);
        write_markdown_table_row(
            &mut out,
            [
                inline_code(&candidate.name),
                candidate.kind.clone(),
                inline_code(&location),
            ],
        );
    }
    out.push('\n');
    out
}
