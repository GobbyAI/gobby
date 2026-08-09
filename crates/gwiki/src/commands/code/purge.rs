use std::collections::BTreeSet;
use std::path::Path;

use serde::Serialize;

use super::doc_paths::{
    collect_generated_doc_pages, is_catalog_owned_code_page, prune_empty_doc_dirs,
    reject_symlinked_doc_path, safe_doc_path,
};
use super::io::read_codewiki_meta;
use super::runtime::{self as output, CodeEngineRuntime};
use super::truth_digest::TRUTH_DIGEST_META_PATH;
use super::{CODEWIKI_META_PATH, OWNERSHIP_META_PATH};

#[derive(Debug, Serialize)]
pub struct CodewikiPurgeSummary {
    pub command: &'static str,
    pub project_id: String,
    pub project_root: String,
    pub out_dir: String,
    pub markdown_removed: usize,
    pub metadata_removed: usize,
}

#[derive(Debug, Default, PartialEq, Eq)]
pub(crate) struct CodewikiPurgeCounts {
    pub(crate) markdown_removed: usize,
    pub(crate) metadata_removed: usize,
}

pub(crate) fn purge_summary(
    ctx: &CodeEngineRuntime,
    out: Option<String>,
    force: bool,
) -> anyhow::Result<Option<CodewikiPurgeSummary>> {
    let out_path = output::resolve_output_path(&ctx.project_root, out.as_deref());
    let out_dir = out_path.display().to_string();
    if !force {
        return Ok(None);
    }

    let counts = purge_generated_output(&out_path)?;
    let summary = CodewikiPurgeSummary {
        command: "codewiki purge",
        project_id: ctx.project_id.clone(),
        project_root: ctx.project_root.display().to_string(),
        out_dir,
        markdown_removed: counts.markdown_removed,
        metadata_removed: counts.metadata_removed,
    };
    Ok(Some(summary))
}

pub(crate) fn purge_confirmation(ctx: &CodeEngineRuntime, out: Option<&str>) -> String {
    let out_dir = output::resolve_output_path(&ctx.project_root, out);
    format!(
        "This will purge generated CodeWiki output under {} for project {}. Re-run with --force to confirm.",
        out_dir.display(),
        ctx.project_id
    )
}

pub(crate) fn purge_summary_text(summary: &CodewikiPurgeSummary) -> String {
    format!(
        "purged {} generated Markdown pages and {} metadata files from {}",
        summary.markdown_removed, summary.metadata_removed, summary.out_dir
    )
}

pub(crate) fn purge_generated_output(out_dir: &Path) -> anyhow::Result<CodewikiPurgeCounts> {
    let meta = read_codewiki_meta(out_dir)?;
    let mut generated_docs = BTreeSet::new();
    generated_docs.extend(meta.docs.keys().cloned());
    generated_docs.extend(meta.generated_docs);
    generated_docs.extend(collect_generated_doc_pages(out_dir)?);

    let mut counts = CodewikiPurgeCounts::default();
    for doc_path in generated_docs {
        if !doc_path.ends_with(".md") || is_catalog_owned_code_page(&doc_path) {
            continue;
        }
        if remove_generated_path(out_dir, &doc_path)? {
            counts.markdown_removed += 1;
        }
    }

    for metadata_path in [
        CODEWIKI_META_PATH,
        OWNERSHIP_META_PATH,
        TRUTH_DIGEST_META_PATH,
    ] {
        if remove_generated_path(out_dir, metadata_path)? {
            counts.metadata_removed += 1;
        }
    }

    Ok(counts)
}

fn remove_generated_path(out_dir: &Path, relative_path: &str) -> anyhow::Result<bool> {
    let target = safe_doc_path(out_dir, relative_path)?;
    reject_symlinked_doc_path(out_dir, &target)?;
    match std::fs::remove_file(&target) {
        Ok(()) => {
            prune_empty_doc_dirs(out_dir, &target)?;
            Ok(true)
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(error.into()),
    }
}
