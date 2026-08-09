use super::*;

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum DiagramOutcome {
    Emitted(String),
    SparseEvidence,
    NoGenerator,
    Rejected,
}

impl DiagramOutcome {
    fn label(&self) -> &'static str {
        match self {
            Self::Emitted(_) => "emitted",
            Self::SparseEvidence => "sparse_evidence",
            Self::NoGenerator => "no_generator",
            Self::Rejected => "rejected",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) enum DiagramKind {
    ModuleDependency,
    ModuleCallSequence,
    CuratedFlow,
}

impl DiagramKind {
    fn label(self) -> &'static str {
        match self {
            Self::ModuleDependency => "module_dependency",
            Self::ModuleCallSequence => "module_call_sequence",
            Self::CuratedFlow => "curated_flow",
        }
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct DiagramStats {
    pub(crate) emitted: usize,
    pub(crate) sparse_evidence: usize,
    pub(crate) no_generator: usize,
    pub(crate) rejected: usize,
    #[serde(default)]
    pub(crate) partial: bool,
    #[serde(skip)]
    recorded_slots: BTreeSet<(String, DiagramKind)>,
}

impl DiagramStats {
    pub(crate) fn record(
        &mut self,
        page_path: &str,
        kind: DiagramKind,
        outcome: &DiagramOutcome,
        progress: &mut CodewikiProgress,
    ) {
        self.record_with_pass(page_path, kind, outcome, None, progress);
    }

    pub(crate) fn record_named_pass(
        &mut self,
        page_path: &str,
        kind: DiagramKind,
        outcome: &DiagramOutcome,
        pass: &str,
        progress: &mut CodewikiProgress,
    ) {
        self.record_with_pass(page_path, kind, outcome, Some(pass), progress);
    }

    fn record_with_pass(
        &mut self,
        page_path: &str,
        kind: DiagramKind,
        outcome: &DiagramOutcome,
        pass: Option<&str>,
        progress: &mut CodewikiProgress,
    ) {
        if !self.recorded_slots.insert((page_path.to_string(), kind)) {
            return;
        }
        match outcome {
            DiagramOutcome::Emitted(_) => self.emitted += 1,
            DiagramOutcome::SparseEvidence => self.sparse_evidence += 1,
            DiagramOutcome::NoGenerator => self.no_generator += 1,
            DiagramOutcome::Rejected => self.rejected += 1,
        }
        let pass = pass.map_or(String::new(), |pass| format!(" ({pass})"));
        progress.emit(format!(
            "diagram {page_path} [{}]: {}{pass}",
            kind.label(),
            outcome.label(),
        ));
    }

    pub(crate) fn total(&self) -> usize {
        self.emitted + self.sparse_evidence + self.no_generator + self.rejected
    }

    pub(crate) fn recorded_slots_len(&self) -> usize {
        self.recorded_slots.len()
    }
}

/// LLM attempts per diagram: one composition plus one repair re-prompt.
const COMPOSE_ATTEMPTS: usize = 2;

/// Compose one evidence-grounded flowchart.
pub(crate) fn compose_flowchart(
    generate: &mut Option<&mut TextGenerator<'_>>,
    evidence: &DiagramEvidence,
    context: &str,
) -> DiagramOutcome {
    if evidence.is_sparse() {
        return DiagramOutcome::SparseEvidence;
    }
    if generate.is_none() {
        return DiagramOutcome::NoGenerator;
    }

    let base_prompt = format!(
        "Compose one Mermaid flowchart for: {context}.\n\n{}",
        evidence.prompt_block()
    );
    let mut feedback: Option<String> = None;
    let mut best: Option<VerifiedFlowchart> = None;

    for _ in 0..COMPOSE_ATTEMPTS {
        let prompt = match &feedback {
            Some(feedback) => format!(
                "{base_prompt}\nYour previous attempt failed verification:\n{feedback}\n\
                 Redraw the flowchart using only the supplied node ids and evidence edges."
            ),
            None => base_prompt.clone(),
        };
        let mut no_tool_loop: Option<&mut ToolLoopGenerator<'_>> = None;
        let Ok(aggregate) = generate_aggregate(
            &mut no_tool_loop,
            generate,
            &prompt,
            prompts::FLOW_DIAGRAM_SYSTEM,
            context,
        ) else {
            break;
        };
        let candidate = match aggregate.content {
            GenerationContent::Generated(text) => text,
            GenerationContent::Failed(_) | GenerationContent::Skipped => break,
        };

        let (verified, issues) = verify_candidate(&candidate, evidence);
        if let Some(verified) = verified {
            if issues.is_empty() {
                return normalize(&verified, evidence)
                    .map(DiagramOutcome::Emitted)
                    .unwrap_or(DiagramOutcome::Rejected);
            }
            if best
                .as_ref()
                .is_none_or(|best| verified.edges.len() > best.edges.len())
            {
                best = Some(verified);
            }
        }
        feedback = Some(
            issues
                .iter()
                .map(|issue| format!("- {issue}"))
                .collect::<Vec<_>>()
                .join("\n"),
        );
    }

    best.and_then(|verified| normalize(&verified, evidence))
        .map(DiagramOutcome::Emitted)
        .unwrap_or(DiagramOutcome::Rejected)
}
