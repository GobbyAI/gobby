use std::path::PathBuf;

use crate::ScopeIdentity;
use crate::commands::ask::assembly::ask_output_from_search;
use crate::output::{AskOutput, SearchOutput, SearchResultOutput, SearchResultType};

pub(super) fn output_with_hooks_hit() -> AskOutput {
    ask_output_from_search(SearchOutput::new(
        ScopeIdentity::topic("docs"),
        "How do hooks work?",
        10,
        vec![SearchResultOutput {
            title: Some("Hooks".to_string()),
            fusion_key: "topic:docs:wiki/hooks.md".to_string(),
            wiki_page: PathBuf::from("wiki/hooks.md"),
            source_path: PathBuf::from("raw/hooks.md"),
            result_type: SearchResultType::Wiki,
            snippet: "Hooks run at turn boundaries and dispatch envelopes to the daemon."
                .to_string(),
            score: 0.9,
            sources: vec!["bm25".to_string()],
            explanations: Vec::new(),
        }],
        Vec::new(),
    ))
}
