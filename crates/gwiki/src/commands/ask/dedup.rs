use std::collections::HashSet;

use crate::output::{AskCodeCitationOutput, AskCodeEdgeOutput, AskOutput};

pub(super) struct AskOutputDedup {
    sources: HashSet<String>,
    degraded_sources: HashSet<String>,
    truncated_components: HashSet<String>,
    warnings: HashSet<String>,
    code_edges: HashSet<CodeEdgeKey>,
    code_citations: HashSet<CodeCitationKey>,
}

type CodeEdgeKey = (String, String, String, String, Option<usize>, String);
type CodeCitationKey = (String, Option<usize>, Option<String>);

impl AskOutputDedup {
    pub(super) fn from_output(output: &AskOutput) -> Self {
        Self {
            sources: output.sources.iter().cloned().collect(),
            degraded_sources: output.degraded_sources.iter().cloned().collect(),
            truncated_components: output.truncated_components.iter().cloned().collect(),
            warnings: output.warnings.iter().cloned().collect(),
            code_edges: output.code_edges.iter().map(code_edge_key).collect(),
            code_citations: output
                .code_citations
                .iter()
                .map(code_citation_key)
                .collect(),
        }
    }

    pub(super) fn push_source(&mut self, output: &mut AskOutput, source: String) {
        if self.sources.insert(source.clone()) {
            output.sources.push(source);
        }
    }

    pub(super) fn push_degraded_source(&mut self, output: &mut AskOutput, source: String) {
        if self.degraded_sources.insert(source.clone()) {
            output.degraded_sources.push(source.clone());
        }
        self.push_warning(output, source);
    }

    pub(super) fn push_truncated_component(&mut self, output: &mut AskOutput, component: String) {
        if self.truncated_components.insert(component.clone()) {
            output.truncated_components.push(component);
        }
    }

    pub(super) fn push_warning(&mut self, output: &mut AskOutput, warning: String) {
        if self.warnings.insert(warning.clone()) {
            output.warnings.push(warning);
        }
    }

    pub(super) fn push_code_edge(&mut self, output: &mut AskOutput, edge: AskCodeEdgeOutput) {
        if self.code_edges.insert(code_edge_key(&edge)) {
            output.code_edges.push(edge);
        }
    }

    pub(super) fn push_code_citation(
        &mut self,
        output: &mut AskOutput,
        citation: AskCodeCitationOutput,
    ) {
        if self.code_citations.insert(code_citation_key(&citation)) {
            self.push_source(output, citation.file.clone());
            output.code_citations.push(citation);
        }
    }
}

pub(super) fn ordered_unique_strings(values: Vec<String>) -> Vec<String> {
    let mut seen = HashSet::new();
    values
        .into_iter()
        .filter(|value| seen.insert(value.clone()))
        .collect()
}

fn code_edge_key(edge: &AskCodeEdgeOutput) -> CodeEdgeKey {
    (
        edge.source.clone(),
        edge.target.clone(),
        edge.kind.clone(),
        edge.direction.clone(),
        edge.line,
        edge.provenance.clone(),
    )
}

fn code_citation_key(citation: &AskCodeCitationOutput) -> CodeCitationKey {
    (
        citation.file.clone(),
        citation.line,
        citation.symbol.clone(),
    )
}

pub(super) fn mark_degraded_source(
    output: &mut AskOutput,
    dedup: &mut AskOutputDedup,
    source: &str,
) {
    output.degraded = true;
    dedup.push_degraded_source(output, source.to_string());
}
