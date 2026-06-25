use std::collections::BTreeMap;
use std::path::Path;

use chrono::{SecondsFormat, Utc};

use super::{
    CodewikiTruthDigest, CodewikiTruthStackEntry, CodewikiTruthSuperseded, DocPruneScope,
    SystemModel, build::infra_descriptor, io::write_doc, structural_repo_summary,
};

pub(crate) const TRUTH_DIGEST_META_PATH: &str = "_meta/truth_digest.json";

const SCHEMA_VERSION: u8 = 1;
const STACK_AUTHORITY_COMPLETE: &str = "complete_current_set";
const STACK_AUTHORITY_PARTIAL: &str = "partial";
const MAX_STACK_ENTRIES: usize = 12;
const MAX_PULL_IN_BY: usize = 1;
const MAX_REPO_SUMMARY_CHARS: usize = 160;
const MAX_FIELD_CHARS: usize = 56;
const MAX_PATH_CHARS: usize = 96;

pub(crate) fn build_truth_digest(
    system_model: &SystemModel,
    project_id: &str,
    file_count: usize,
    module_count: usize,
) -> CodewikiTruthDigest {
    let mut stack = system_model
        .services
        .iter()
        .map(|boundary| {
            let descriptor = infra_descriptor(boundary.kind);
            CodewikiTruthStackEntry {
                service: bounded(&boundary.name, MAX_FIELD_CHARS),
                kind: boundary.kind.kind_slug().to_string(),
                adapter_module: bounded(descriptor.adapter_module, MAX_PATH_CHARS),
                pulled_in_by: boundary
                    .pulled_in_by
                    .iter()
                    .take(MAX_PULL_IN_BY)
                    .map(|source| bounded(source, MAX_FIELD_CHARS))
                    .collect(),
                summary: bounded(descriptor.summary, MAX_FIELD_CHARS),
                degradation: bounded(descriptor.degradation, MAX_FIELD_CHARS),
            }
        })
        .collect::<Vec<_>>();
    stack.sort_by(|a, b| a.service.cmp(&b.service));
    stack.truncate(MAX_STACK_ENTRIES);

    let key_paths = stack
        .iter()
        .map(|entry| (entry.service.clone(), entry.adapter_module.clone()))
        .collect::<BTreeMap<_, _>>();
    let stack_authority = if stack.is_empty() || !system_model.notes.is_empty() {
        STACK_AUTHORITY_PARTIAL
    } else {
        STACK_AUTHORITY_COMPLETE
    };

    CodewikiTruthDigest {
        schema_version: SCHEMA_VERSION,
        generated_at: Utc::now().to_rfc3339_opts(SecondsFormat::Secs, true),
        project_id: project_id.to_string(),
        repo_summary: bounded(
            &structural_repo_summary(file_count, module_count),
            MAX_REPO_SUMMARY_CHARS,
        ),
        stack_authority: stack_authority.to_string(),
        stack,
        key_paths,
        superseded: Vec::<CodewikiTruthSuperseded>::new(),
    }
}

pub(crate) fn write_truth_digest(
    out_dir: &Path,
    doc_scope: &DocPruneScope,
    digest: &CodewikiTruthDigest,
) -> anyhow::Result<()> {
    if !doc_scope.is_unscoped() {
        return Ok(());
    }
    let content = serde_json::to_string(digest)?;
    write_doc(out_dir, TRUTH_DIGEST_META_PATH, &(content + "\n"))
}

fn bounded(value: &str, max_chars: usize) -> String {
    let value = value.trim();
    if value.chars().count() <= max_chars {
        return value.to_string();
    }
    let keep = max_chars.saturating_sub(3);
    let mut truncated = value.chars().take(keep).collect::<String>();
    truncated.push_str("...");
    truncated
}
