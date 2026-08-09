use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write as _;

use gobby_core::vault::mermaid::{escape_label as mermaid_label, is_valid_mermaid};
use serde::{Deserialize, Serialize};

use super::types::TextGenerator;
use super::{CodewikiProgress, GenerationContent, ToolLoopGenerator, generate_aggregate, prompts};

mod candidates;
mod evidence;
mod generation;

#[cfg(test)]
use candidates::parse_arrow;
use candidates::{VerifiedFlowchart, normalize, verify_candidate};
pub(crate) use evidence::{DiagramEvidence, EvidenceEdge, NodeShape};
pub(crate) use generation::{DiagramKind, DiagramOutcome, DiagramStats, compose_flowchart};

#[cfg(test)]
mod tests {
    use super::super::CodewikiProgress;
    use super::super::types::PromptTier;
    use super::*;

    fn evidence() -> DiagramEvidence {
        let mut evidence = DiagramEvidence::default();
        evidence.push_node("a", "Alpha", NodeShape::Box);
        evidence.push_node("b", "Beta (core)", NodeShape::Stadium);
        evidence.push_node("c", "Gamma", NodeShape::Cylinder);
        evidence.push_edge("a", "b", None, false);
        evidence.push_edge("b", "c", Some("required".to_string()), true);
        evidence
    }

    fn compose_with(responses: Vec<String>, evidence: &DiagramEvidence) -> Option<String> {
        let mut responses = responses;
        let mut generator = move |_prompt: &str, system: &str, _tier: PromptTier| {
            assert_eq!(system, prompts::FLOW_DIAGRAM_SYSTEM);
            (!responses.is_empty()).then(|| responses.remove(0))
        };
        let mut generate: Option<&mut TextGenerator<'_>> = Some(&mut generator);
        match compose_flowchart(&mut generate, evidence, "test flow") {
            DiagramOutcome::Emitted(block) => Some(block),
            DiagramOutcome::SparseEvidence
            | DiagramOutcome::NoGenerator
            | DiagramOutcome::Rejected => None,
        }
    }

    fn compose_outcome_with(responses: Vec<String>, evidence: &DiagramEvidence) -> DiagramOutcome {
        let mut responses = responses;
        let mut generator = move |_prompt: &str, system: &str, _tier: PromptTier| {
            assert_eq!(system, prompts::FLOW_DIAGRAM_SYSTEM);
            (!responses.is_empty()).then(|| responses.remove(0))
        };
        let mut generate: Option<&mut TextGenerator<'_>> = Some(&mut generator);
        compose_flowchart(&mut generate, evidence, "test flow")
    }

    #[test]
    fn diagram_outcomes_cover_every_terminal_state() {
        let emitted =
            compose_outcome_with(vec!["flowchart LR\n    a --> b\n".to_string()], &evidence());
        assert!(matches!(emitted, DiagramOutcome::Emitted(_)));

        let mut sparse = DiagramEvidence::default();
        sparse.push_node("a", "Alpha", NodeShape::Box);
        let sparse = compose_outcome_with(Vec::new(), &sparse);
        assert_eq!(sparse, DiagramOutcome::SparseEvidence);

        let mut generate: Option<&mut TextGenerator<'_>> = None;
        assert_eq!(
            compose_flowchart(&mut generate, &evidence(), "test flow"),
            DiagramOutcome::NoGenerator
        );

        let prose = "Here is a diagram description instead of a diagram.".to_string();
        let rejected = compose_outcome_with(vec![prose.clone(), prose], &evidence());
        assert_eq!(rejected, DiagramOutcome::Rejected);
    }

    #[test]
    fn diagram_outcomes_record_one_final_log_per_unique_slot() {
        let mut stats = DiagramStats::default();
        let mut progress = CodewikiProgress::capture();

        stats.record(
            "code/concepts/runtime.md",
            DiagramKind::CuratedFlow,
            &DiagramOutcome::SparseEvidence,
            &mut progress,
        );
        stats.record(
            "code/concepts/runtime.md",
            DiagramKind::CuratedFlow,
            &DiagramOutcome::Emitted("ignored duplicate".to_string()),
            &mut progress,
        );
        stats.record(
            "code/modules/runtime.md",
            DiagramKind::ModuleDependency,
            &DiagramOutcome::Emitted("diagram".to_string()),
            &mut progress,
        );
        stats.record(
            "code/modules/runtime.md",
            DiagramKind::ModuleCallSequence,
            &DiagramOutcome::NoGenerator,
            &mut progress,
        );

        assert_eq!(stats.emitted, 1);
        assert_eq!(stats.sparse_evidence, 1);
        assert_eq!(stats.no_generator, 1);
        assert_eq!(stats.rejected, 0);
        assert_eq!(stats.total(), 3);
        assert_eq!(
            progress.into_lines(),
            vec![
                "codewiki: diagram code/concepts/runtime.md [curated_flow]: sparse_evidence",
                "codewiki: diagram code/modules/runtime.md [module_dependency]: emitted",
                "codewiki: diagram code/modules/runtime.md [module_call_sequence]: no_generator",
            ]
        );
    }

    #[test]
    fn composes_normalized_block_from_clean_model_output() {
        let block = compose_with(
            vec!["flowchart LR\n    a --> b\n    b -.->|required| c\n".to_string()],
            &evidence(),
        )
        .expect("diagram");
        assert!(block.starts_with("```mermaid\nflowchart LR\n"));
        // Labels come from evidence with Mermaid-native escaping, shapes from
        // the evidence node kinds.
        assert!(block.contains("a[\"Alpha\"]"));
        assert!(block.contains("b([\"Beta #40;core#41;\"])"));
        assert!(block.contains("c[(\"Gamma\")]"));
        // Canonical arrow style and label re-attach from evidence.
        assert!(block.contains("b -.->|\"required\"| c"));
        assert!(is_valid_mermaid(&block));
    }

    #[test]
    fn strips_a_model_added_mermaid_fence() {
        let block = compose_with(
            vec!["```mermaid\nflowchart TD\n    a --> b\n```".to_string()],
            &evidence(),
        )
        .expect("diagram");
        assert!(block.starts_with("```mermaid\nflowchart TD\n"));
        assert_eq!(block.matches("```").count(), 2);
    }

    #[test]
    fn unevidenced_arrow_is_rejected_and_repair_prompt_names_it() {
        // First attempt draws an arrow that matches no evidence edge (reversed
        // direction); the repair prompt must name it and the second, clean
        // attempt wins.
        let mut prompts_seen: Vec<String> = Vec::new();
        let mut responses = vec![
            "flowchart TD\n    a --> b\n    c --> b\n".to_string(),
            "flowchart TD\n    a --> b\n    b -.-> c\n".to_string(),
        ];
        let mut generator = |prompt: &str, _system: &str, _tier: PromptTier| {
            prompts_seen.push(prompt.to_string());
            (!responses.is_empty()).then(|| responses.remove(0))
        };
        let mut generate: Option<&mut TextGenerator<'_>> = Some(&mut generator);
        let DiagramOutcome::Emitted(block) =
            compose_flowchart(&mut generate, &evidence(), "test flow")
        else {
            panic!("diagram was not emitted");
        };

        assert_eq!(prompts_seen.len(), 2, "one repair re-prompt");
        assert!(
            prompts_seen[1].contains("arrow 'c --> b' matches no supplied evidence edge"),
            "repair prompt must name the unevidenced arrow: {}",
            prompts_seen[1]
        );
        assert!(block.contains("a --> b"));
        assert!(
            !block.contains("c --> b"),
            "unevidenced arrow rejected: {block}"
        );
    }

    #[test]
    fn unevidenced_arrow_is_dropped_when_repair_also_fails() {
        // Both attempts keep the unevidenced arrow: the deterministic repair
        // emits only what survived edge verification.
        let bad = "flowchart TD\n    a --> b\n    c --> a\n".to_string();
        let block = compose_with(vec![bad.clone(), bad], &evidence()).expect("diagram");
        assert!(block.contains("a --> b"));
        assert!(!block.contains("c --> a"));
    }

    #[test]
    fn nothing_survives_means_no_diagram() {
        let bad = "flowchart TD\n    c --> a\n    b --> a\n".to_string();
        assert_eq!(compose_with(vec![bad.clone(), bad], &evidence()), None);
    }

    #[test]
    fn unknown_node_is_rejected() {
        let bad = "flowchart TD\n    a --> b\n    ghost --> b\n".to_string();
        let block = compose_with(vec![bad.clone(), bad], &evidence()).expect("diagram");
        assert!(!block.contains("ghost"));
    }

    #[test]
    fn disconnected_island_is_dropped() {
        let mut evidence = evidence();
        evidence.push_node("x", "Xi", NodeShape::Box);
        evidence.push_node("y", "Ypsilon", NodeShape::Box);
        evidence.push_edge("x", "y", None, false);
        // The model draws two disconnected components; only the larger stays.
        let drawing = "flowchart TD\n    a --> b\n    b -.-> c\n    x --> y\n".to_string();
        let block = compose_with(vec![drawing.clone(), drawing], &evidence).expect("diagram");
        assert!(block.contains("a --> b"));
        assert!(
            !block.contains("x --> y"),
            "island must be dropped: {block}"
        );
        assert!(!block.contains("Xi"));
    }

    #[test]
    fn sparse_evidence_or_missing_generator_yields_none() {
        let mut generate: Option<&mut TextGenerator<'_>> = None;
        assert_eq!(
            compose_flowchart(&mut generate, &evidence(), "test"),
            DiagramOutcome::NoGenerator
        );

        let mut no_edges = DiagramEvidence::default();
        no_edges.push_node("a", "Alpha", NodeShape::Box);
        no_edges.push_node("b", "Beta", NodeShape::Box);
        assert_eq!(
            compose_with(vec!["flowchart TD\n    a --> b\n".to_string()], &no_edges),
            None
        );
    }

    #[test]
    fn non_flowchart_output_yields_none() {
        let prose = "Here is a diagram description instead of a diagram.".to_string();
        assert_eq!(compose_with(vec![prose.clone(), prose], &evidence()), None);
    }

    #[test]
    fn tolerated_structure_lines_are_dropped_not_fatal() {
        let drawing = "flowchart TD\n    %% comment\n    subgraph g [\"Group\"]\n    a[\"Alpha\"]\n    end\n    a --> b\n    classDef svc fill:#eef;\n".to_string();
        let block = compose_with(vec![drawing], &evidence()).expect("diagram");
        assert!(!block.contains("subgraph"));
        assert!(!block.contains("classDef"));
        assert!(block.contains("a --> b"));
    }

    #[test]
    fn evidence_prompt_block_lists_nodes_and_edges() {
        let block = evidence().prompt_block();
        assert!(block.contains("- a: Alpha"));
        assert!(block.contains("- a -> b"));
        assert!(block.contains("- b -> c (required)"));
    }

    #[test]
    #[should_panic(expected = "diagram evidence edge references missing target node `ghost`")]
    fn evidence_edges_must_reference_existing_nodes() {
        let mut evidence = DiagramEvidence::default();
        evidence.push_node("a", "Alpha", NodeShape::Box);
        evidence.push_edge("a", "ghost", None, false);
    }

    #[test]
    fn failed_repair_attempt_preserves_best_surviving_candidate() {
        let mut responses = vec![Some("flowchart TD\n    a --> b\n    c --> a\n".to_string())];
        let mut generator =
            |_prompt: &str, _system: &str, _tier: PromptTier| responses.pop().flatten();
        let mut generate: Option<&mut TextGenerator<'_>> = Some(&mut generator);

        let DiagramOutcome::Emitted(block) =
            compose_flowchart(&mut generate, &evidence(), "test flow")
        else {
            panic!("diagram was not emitted");
        };

        assert!(block.contains("a --> b"));
        assert!(!block.contains("c --> a"));
    }

    #[test]
    fn worse_partial_repair_does_not_replace_best_candidate() {
        let mut evidence = evidence();
        evidence.push_node("x", "Xi", NodeShape::Box);
        evidence.push_node("y", "Ypsilon", NodeShape::Box);
        evidence.push_edge("x", "y", None, false);
        let mut responses = vec![
            "flowchart TD\n    a --> b\n    b -.-> c\n    x --> y\n".to_string(),
            "flowchart TD\n    a --> b\n    c --> a\n".to_string(),
        ];
        let mut generator = |_prompt: &str, _system: &str, _tier: PromptTier| {
            (!responses.is_empty()).then(|| responses.remove(0))
        };
        let mut generate: Option<&mut TextGenerator<'_>> = Some(&mut generator);

        let DiagramOutcome::Emitted(block) =
            compose_flowchart(&mut generate, &evidence, "test flow")
        else {
            panic!("diagram was not emitted");
        };

        assert!(block.contains("a --> b"));
        assert!(
            block.contains("b -.->"),
            "best two-edge survivor should win over one-edge repair: {block}"
        );
        assert!(!block.contains("x --> y"));
        assert!(!block.contains("c --> a"));
    }

    #[test]
    fn parse_arrow_accepts_subset_and_rejects_chains() {
        assert_eq!(
            parse_arrow("a --> b"),
            Some(("a".to_string(), "b".to_string()))
        );
        assert_eq!(
            parse_arrow("a[\"X\"] -.->|\"lbl\"| b([\"Y\"])"),
            Some(("a".to_string(), "b".to_string()))
        );
        assert_eq!(
            parse_arrow("a ==> b"),
            Some(("a".to_string(), "b".to_string()))
        );
        assert_eq!(parse_arrow("a --> b --> c"), None);
        assert_eq!(parse_arrow("a & b --> c"), None);
        assert_eq!(parse_arrow("a --- b"), None);
    }
}
