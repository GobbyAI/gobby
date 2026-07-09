use super::{AuditReport, ClaimClassification};

pub fn render_text(report: &AuditReport) -> String {
    let mut text = format!("Wiki audit report\nScope: {}\n", report.scope);
    let mut extracted = 0usize;
    let mut inferred = 0usize;
    let mut ambiguous = 0usize;
    for claim in &report.claims {
        match claim.classification {
            ClaimClassification::Extracted => extracted += 1,
            ClaimClassification::Inferred => inferred += 1,
            ClaimClassification::Ambiguous => ambiguous += 1,
        }
    }
    text.push_str(&format!(
        "\nClaim classifications: EXTRACTED {extracted} / INFERRED {inferred} / AMBIGUOUS {ambiguous}\n"
    ));
    text.push_str("\nUnsupported claims:\n");
    if report.unsupported_claims.is_empty() {
        text.push_str("- none\n");
    } else {
        for claim in &report.unsupported_claims {
            text.push_str("- ");
            text.push_str(&claim.path.display().to_string());
            text.push(':');
            text.push_str(&claim.line.to_string());
            text.push(' ');
            text.push_str(&claim.claim);
            if !claim.source_context.is_empty() {
                text.push_str(" [sources: ");
                text.push_str(
                    &claim
                        .source_context
                        .iter()
                        .map(|source| source.source_id.clone())
                        .collect::<Vec<_>>()
                        .join(", "),
                );
                text.push(']');
            }
            text.push('\n');
        }
    }
    text
}
