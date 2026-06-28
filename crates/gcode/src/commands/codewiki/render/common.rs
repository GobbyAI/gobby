/// Page-level degraded-source list for a single AI generation attempt. `reason`
/// is the distinct failure reason code (`model-refusal`/`model-prompt-echo`/
/// `model-unavailable`) when the attempt failed, or `None` when it succeeded or
/// was skipped. Splitting the codes out of a blanket `model-unavailable` lets a
/// reader tell a refusal from genuine unavailability.
pub(crate) fn model_degraded_sources(reason: Option<&str>) -> Vec<String> {
    reason.into_iter().map(str::to_string).collect()
}

/// Hard ceiling on an inlined table-cell summary, in characters. A leading
/// paragraph longer than this is truncated on a word boundary with an ellipsis.
const CELL_SUMMARY_CAP: usize = 600;

/// Reduce a full generated summary to a compact, single-line table-cell value.
///
/// Module and subsystem summaries can run to several paragraphs plus embedded
/// Markdown tables (the module's full structured brief lives on its own page).
/// Inlined verbatim into a parent table cell, that becomes an unreadable wall of
/// pipe-escaped pseudo-tables. Keep only the leading prose paragraph — the
/// human-readable gist a navigation row needs — stopping at the first Markdown
/// table row or the first paragraph break, flatten internal whitespace, and
/// hard-cap the length as a runaway guard. The dropped detail is one click away
/// on the row's linked module page.
pub(crate) fn cell_summary(summary: &str) -> String {
    let mut lead = String::new();
    for line in summary.lines() {
        let trimmed = line.trim();
        // A Markdown table row opens the structured brief that belongs on the
        // module's own page, not in a parent cell — stop before it.
        if trimmed.starts_with('|') {
            break;
        }
        if trimmed.is_empty() {
            // A blank line ends the leading paragraph once prose has started.
            if !lead.is_empty() {
                break;
            }
            continue;
        }
        if !lead.is_empty() {
            lead.push(' ');
        }
        lead.push_str(trimmed);
    }

    // Fall back to a whitespace-flattened whole summary when the text opened
    // straight into a table with no leading prose.
    if lead.is_empty() {
        lead = summary.split_whitespace().collect::<Vec<_>>().join(" ");
    }

    if lead.chars().count() <= CELL_SUMMARY_CAP {
        return lead;
    }
    let mut capped: String = lead.chars().take(CELL_SUMMARY_CAP).collect();
    // Prefer a clean cut at the last word boundary inside the cap.
    if let Some(space) = capped.rfind(' ') {
        capped.truncate(space);
    }
    capped.push('…');
    capped
}

#[cfg(test)]
mod tests {
    use super::cell_summary;

    #[test]
    fn cell_summary_keeps_leading_paragraph_and_drops_embedded_table() {
        let summary = "`crates/gcode` owns the code-indexing CLI. It indexes \
             source files and projects facts into graph and vector backends.\n\n\
             | Area | Responsibility |\n| --- | --- |\n| `src` | CLI dispatch. |";

        let cell = cell_summary(summary);

        assert_eq!(
            cell,
            "`crates/gcode` owns the code-indexing CLI. It indexes source files \
             and projects facts into graph and vector backends."
        );
        assert!(
            !cell.contains('|'),
            "table content must not leak into the cell"
        );
    }

    #[test]
    fn cell_summary_stops_at_first_table_row_without_a_blank_line() {
        let summary = "Adapts core capabilities to a local daemon transport.\n\
             | Surface | Role |\n| --- | --- |\n| Op | sends requests. |";

        let cell = cell_summary(summary);

        assert_eq!(
            cell,
            "Adapts core capabilities to a local daemon transport."
        );
    }

    #[test]
    fn cell_summary_flattens_when_text_opens_with_a_table() {
        let summary = "| Key | Value |\n| --- | --- |\n| a | b |";

        let cell = cell_summary(summary);

        assert_eq!(cell, "| Key | Value | | --- | --- | | a | b |");
    }

    #[test]
    fn cell_summary_caps_runaway_single_paragraph_on_a_word_boundary() {
        let word = "alpha ";
        let summary = word.repeat(200); // 1200 chars, no table, no blank line

        let cell = cell_summary(&summary);

        assert!(cell.chars().count() <= super::CELL_SUMMARY_CAP + 1); // +1 for '…'
        assert!(cell.ends_with('…'));
        assert!(!cell.contains("alph…"), "cut must land on a word boundary");
    }
}
