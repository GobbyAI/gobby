use std::fmt::Write as _;

use super::super::*;

/// Render the deterministic infra-stack page (#892). The page is derived purely
/// from the workspace's Cargo manifests and service boundaries — no LLM, no
/// graph, no network — so it carries no source-span provenance and never marks
/// itself degraded. One `##` section per [`InfraSection`], each citing the real
/// adapter module that talks to the service.
pub(crate) fn render_infrastructure_doc(doc: &InfrastructureDoc) -> String {
    let mut out = frontmatter_with_degradation_without_ranges(
        "Infrastructure Stack",
        "code_infrastructure",
        &[],
        &doc.degraded_sources,
    );
    out.push_str("# Infrastructure Stack\n\n");
    out.push_str(
        "This page is derived deterministically from the workspace's Cargo manifests and \
         service boundaries — no LLM. Each section names a service the workspace can reach, \
         what pulls it in, the adapter module that talks to it, and how the workspace behaves \
         when it is unavailable.\n\n",
    );

    for section in &doc.sections {
        let _ = writeln!(out, "## {}\n", section.service);
        let _ = writeln!(out, "{}\n", section.summary);
        let pulled_in_by = if section.pulled_in_by.is_empty() {
            "workspace".to_string()
        } else {
            section.pulled_in_by.join(", ")
        };
        let _ = writeln!(out, "**Pulled in by:** {pulled_in_by}\n");
        let _ = writeln!(
            out,
            "**Code path:** {}\n",
            inline_code(&section.adapter_module)
        );
        let _ = writeln!(out, "**When unavailable:** {}\n", section.degradation);
    }

    out
}
