use super::AuditReport;

pub fn render_text(report: &AuditReport) -> String {
    let mut text = format!("Wiki audit report\nScope: {}\n", report.scope);
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
