use std::fmt::Write as _;

use super::super::*;

pub(crate) fn render_architecture_doc(architecture: &ArchitectureDoc) -> String {
    let mut doc = frontmatter_with_degradation_without_ranges(
        "Architecture Overview",
        "code_architecture",
        &architecture.source_spans,
        &architecture.degraded_sources,
    );
    append_relevant_source_files(&mut doc, &architecture.source_spans);
    doc.push_str("# Architecture Overview\n\n");
    if let Some(narrative) = &architecture.narrative {
        write_section(&mut doc, "Overview", narrative);
    }
    // Model-seeded architectural diagrams (#891). The section string is already
    // built from validated Mermaid fences by the diagram renderer, so it is
    // emitted verbatim; its absence is normal and never marks the page degraded.
    if let Some(diagrams) = &architecture.diagrams {
        doc.push_str(diagrams);
        if !doc.ends_with('\n') {
            doc.push('\n');
        }
    }
    if !architecture.subsystems.is_empty() {
        doc.push_str("## Subsystems\n\n");
        write_markdown_table_header(&mut doc, &["Subsystem", "Responsibility", "Child modules"]);
        for subsystem in &architecture.subsystems {
            let child_modules = if subsystem.child_modules.is_empty() {
                "None".to_string()
            } else {
                subsystem
                    .child_modules
                    .iter()
                    .map(|child| module_wikilink(child))
                    .collect::<Vec<_>>()
                    .join(", ")
            };
            write_markdown_table_row(
                &mut doc,
                [
                    module_wikilink(&subsystem.module),
                    subsystem.responsibility.clone(),
                    child_modules,
                ],
            );
        }
        doc.push('\n');
    }
    doc
}

pub(crate) fn render_onboarding_doc(onboarding: &OnboardingDoc) -> String {
    let mut doc = frontmatter_with_degradation(
        "Start Here",
        "code_onboarding",
        &onboarding.source_spans,
        &onboarding.degraded_sources,
    );
    append_relevant_source_files(&mut doc, &onboarding.source_spans);
    doc.push_str("# Start Here\n\n");

    if !onboarding.entry_points.is_empty() {
        doc.push_str("## Entry Points\n\n");
        for entry in &onboarding.entry_points {
            let _ = writeln!(doc, "- {} - {}", entry.link, entry.description);
        }
        doc.push('\n');
    }

    if onboarding.reading_order.is_empty() {
        // Entry Points above already covers the non-graph fallback.
        return doc;
    }

    doc.push_str("## Recommended Reading Order\n\n");
    for (index, step) in onboarding.reading_order.iter().enumerate() {
        let _ = writeln!(
            doc,
            "{}. {} - {} centrality degree, {:.3} score. {}",
            index + 1,
            module_wikilink(&step.module),
            step.degree,
            step.score,
            step.summary
        );
    }
    doc.push('\n');
    doc
}

pub(crate) fn render_hotspots_doc(hotspots: &HotspotsDoc) -> String {
    let mut doc = frontmatter_with_degradation(
        "Hotspots",
        "code_hotspots",
        &hotspots.source_spans,
        &hotspots.degraded_sources,
    );
    append_relevant_source_files(&mut doc, &hotspots.source_spans);
    doc.push_str("# Hotspots\n\n");

    let hotspot_ids = hotspots
        .hotspots
        .iter()
        .map(|finding| finding.node.id.clone())
        .collect::<BTreeSet<_>>();
    write_hotspot_section(&mut doc, "Hotspots", &hotspots.hotspots);
    write_hotspot_section_with_cross_refs(
        &mut doc,
        "God Nodes",
        &hotspots.god_nodes,
        Some((&hotspot_ids, "Hotspots")),
    );
    write_hotspot_section_with_cross_refs(
        &mut doc,
        "Bridges",
        &hotspots.bridges,
        Some((&hotspot_ids, "Hotspots")),
    );

    if hotspots.hotspots.is_empty() && hotspots.god_nodes.is_empty() && hotspots.bridges.is_empty()
    {
        doc.push_str("No graph hotspots were identified from the current code index.\n\n");
    }

    doc
}

fn write_hotspot_section(doc: &mut String, title: &str, findings: &[HotspotFinding]) {
    write_hotspot_section_with_cross_refs(doc, title, findings, None);
}

fn write_hotspot_section_with_cross_refs(
    doc: &mut String,
    title: &str,
    findings: &[HotspotFinding],
    cross_ref: Option<(&BTreeSet<String>, &str)>,
) {
    doc.push_str("## ");
    doc.push_str(title);
    doc.push_str("\n\n");

    if findings.is_empty() {
        doc.push_str("None identified.\n\n");
        return;
    }

    for finding in findings {
        if let Some((existing_ids, section)) = cross_ref
            && existing_ids.contains(&finding.node.id)
        {
            let _ = writeln!(
                doc,
                "- {} ({}) - also listed under {section}; see that entry for full details.",
                finding.node.wikilink,
                inline_code(&finding.node.label)
            );
            continue;
        }

        let mut details = Vec::new();
        details.push(format!("kind {}", inline_code(&finding.node.kind)));
        details.push(format!("component {}", inline_code(&finding.node.id)));
        if let Some(degree) = finding.degree {
            details.push(format!("degree {degree}"));
        }
        if let Some(score) = finding.score {
            details.push(format!("score {score:.3}"));
        }
        if let Some(frequency) = finding.frequency {
            details.push(format!("frequency {frequency}"));
        }
        if let Some(weight) = finding.weight {
            details.push(format!("weight {weight:.1}"));
        }
        if let Some(file) = &finding.node.file_wikilink {
            details.push(format!("file {file}"));
        }
        if let Some(span) = &finding.node.source_span {
            details.push(span.citation());
        }

        let _ = writeln!(
            doc,
            "- {} ({}) - {}",
            finding.node.wikilink,
            inline_code(&finding.node.label),
            details.join(", ")
        );
    }
    doc.push('\n');
}
