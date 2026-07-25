use std::path::{Path, PathBuf};

use crate::WikiError;
use crate::explainer::{CitationTarget, ExplainerGeneration, ExplainerReport, ground_explainer};

use super::paths::{
    ensure_synthesized_path_inside_vault, relative_path, source_links, source_page_paths, wiki_link,
};
use super::render::{
    FrontmatterFields, render_frontmatter, render_list_section, render_source_excerpts,
};
use super::types::{ArticleKind, SynthesisInput, SynthesizedPage};

pub fn synthesize_article(
    vault_root: &Path,
    input: &SynthesisInput,
    article_path: PathBuf,
    explainer: &ExplainerGeneration,
) -> Result<SynthesizedPage, WikiError> {
    // Callers resolve the path up front (explicit target page or
    // `resolve_article_path`) so recompiles can feed the existing page body
    // into the synthesis prompt before this point.
    let path = article_path;
    ensure_synthesized_path_inside_vault(vault_root, &path, "article_path")?;

    let source_paths = source_page_paths(vault_root, &path, &input.accepted_sources);
    let source_links = source_links(vault_root, &input.accepted_sources, &source_paths);
    let (explainer_body, report) =
        ground_article_explainer(vault_root, input, &source_links, explainer);
    let degraded_sources: &[&str] = if report.status == "failed" {
        &["model_provider_unavailable"]
    } else {
        &[]
    };

    let mut markdown = String::new();
    render_frontmatter(
        &mut markdown,
        &FrontmatterFields {
            title: &input.topic,
            source_kind: input.target_kind.source_kind(),
            handoff_id: &input.handoff_id,
            synthesis_mode: report.route.unwrap_or("fallback"),
            degraded_sources,
            aliases: &input.aliases,
            extra_tags: &input.extra_tags,
            candidate: input.candidate,
            source_path: None,
        },
    );
    markdown.push_str("# ");
    markdown.push_str(&input.topic);
    markdown.push_str("\n\n");

    if !source_links.is_empty() {
        markdown.push_str("Sources: ");
        markdown.push_str(&source_links.join(", "));
        markdown.push_str("\n\n");
    }

    match &explainer_body {
        Some(body) => {
            markdown.push_str(body);
            if !markdown.ends_with("\n\n") {
                markdown.push('\n');
                if !markdown.ends_with("\n\n") {
                    markdown.push('\n');
                }
            }
        }
        None => {
            let headings = if input.outline.is_empty() {
                vec!["Overview".to_string()]
            } else {
                input.outline.clone()
            };
            for heading in &headings {
                markdown.push_str("## ");
                markdown.push_str(heading);
                markdown.push_str("\n\n");
            }
        }
    }
    markdown.push_str("## Source excerpts\n\n");
    render_source_excerpts(&mut markdown, &input.accepted_sources);

    render_list_section(&mut markdown, "Citations", &input.citations);
    render_list_section(
        &mut markdown,
        "Conflicting claims",
        &input.conflicting_claims,
    );
    render_list_section(&mut markdown, "Missing evidence", &input.missing_evidence);
    if !source_links.is_empty() {
        render_list_section(&mut markdown, "Backlinks", &source_links);
    }

    let source_hashes = input
        .accepted_sources
        .iter()
        .map(|source| source.source_hash.as_str());
    let markdown = crate::page_version::stamp_generated_page(&markdown, source_hashes);

    Ok(SynthesizedPage {
        path,
        title: input.topic.clone(),
        markdown,
        explainer: Some(report),
    })
}

/// Ground the generated explainer against the accepted sources and produce
/// the per-article report. Failure keeps the structural skeleton (`None`
/// body) and is reported as degraded; skipped synthesis is structural by
/// intent and records nothing.
fn ground_article_explainer(
    vault_root: &Path,
    input: &SynthesisInput,
    source_links: &[String],
    explainer: &ExplainerGeneration,
) -> (Option<String>, ExplainerReport) {
    match explainer {
        ExplainerGeneration::Generated {
            body,
            model,
            route,
            tool_use_count,
            turns,
            usage,
        } => {
            let targets: Vec<CitationTarget> = input
                .accepted_sources
                .iter()
                .zip(source_links)
                .map(|(source, link)| CitationTarget {
                    key: relative_path(vault_root, &source.path),
                    link: link.clone(),
                    corpus: format!(
                        "{} {}",
                        source.title,
                        source.chunks.first().map(String::as_str).unwrap_or("")
                    ),
                })
                .collect();
            let grounded = ground_explainer(body, &targets);
            let report = ExplainerReport {
                status: "generated",
                route: Some(*route),
                model: model.clone(),
                error: None,
                tool_use_count: *tool_use_count,
                turns: *turns,
                usage: usage.clone(),
                citations_kept: grounded.citations_kept,
                citations_stripped: grounded.citations_stripped,
                fallback_sections: grounded.fallback_sections,
            };
            (Some(grounded.body), report)
        }
        ExplainerGeneration::Failed { error } => {
            let mut report = ExplainerReport::skipped();
            report.status = "failed";
            report.error = Some(error.clone());
            (None, report)
        }
        ExplainerGeneration::Skipped => (None, ExplainerReport::skipped()),
    }
}

pub fn synthesize_source_pages(
    vault_root: &Path,
    input: &SynthesisInput,
    article_path: &Path,
) -> Result<Vec<SynthesizedPage>, WikiError> {
    let article_link = wiki_link(vault_root, article_path, &input.topic);
    let source_paths = source_page_paths(vault_root, article_path, &input.accepted_sources);
    let mut pages = Vec::with_capacity(input.accepted_sources.len());
    for (source, path) in input.accepted_sources.iter().zip(source_paths) {
        ensure_synthesized_path_inside_vault(vault_root, &path, "source_path")?;
        if source.existing_page.is_some() {
            // The source already has a rich manifest-backed digest at this
            // path; the article links there instead of duplicating a stub.
            continue;
        }
        let identity = relative_path(vault_root, &source.path);
        // Recompiles resolve back to the stub written for this source; keep
        // the "Used by" links other articles already recorded on it.
        let mut used_by = existing_used_by_links(&path);
        if !used_by.contains(&article_link) {
            used_by.push(article_link.clone());
        }
        let mut markdown = String::new();
        render_frontmatter(
            &mut markdown,
            &FrontmatterFields {
                title: &source.title,
                source_kind: ArticleKind::Source.source_kind(),
                handoff_id: &input.handoff_id,
                synthesis_mode: "source",
                degraded_sources: &[],
                aliases: &[],
                extra_tags: &[],
                // Source digest stubs are extraction artifacts, never
                // LLM-proposed knowledge — they are not quarantined.
                candidate: false,
                source_path: Some(&identity),
            },
        );
        markdown.push_str("# ");
        markdown.push_str(&source.title);
        markdown.push_str("\n\n");
        markdown.push_str("Source path: `");
        markdown.push_str(&identity);
        markdown.push_str("`\n\n");
        render_list_section(&mut markdown, "Extracts", &source.chunks);
        render_list_section(&mut markdown, "Used by", &used_by);
        let markdown = crate::page_version::stamp_generated_page(
            &markdown,
            std::iter::once(source.source_hash.as_str()),
        );

        pages.push(SynthesizedPage {
            path,
            title: source.title.clone(),
            markdown,
            explainer: None,
        });
    }
    Ok(pages)
}

/// Bullet entries under the `## Used by` section of an existing source stub
/// page, so a rewrite preserves links from other compiled articles. Missing
/// or unreadable pages yield no entries.
fn existing_used_by_links(page_path: &Path) -> Vec<String> {
    let Ok(markdown) = std::fs::read_to_string(page_path) else {
        return Vec::new();
    };
    let mut links = Vec::new();
    let mut in_section = false;
    for line in markdown.lines() {
        let trimmed = line.trim();
        if let Some(heading) = trimmed.strip_prefix("## ") {
            in_section = heading.trim() == "Used by";
            continue;
        }
        if !in_section {
            continue;
        }
        if let Some(value) = trimmed.strip_prefix("- ") {
            let value = value.trim();
            if !value.is_empty() && value != "None recorded." {
                links.push(value.to_string());
            }
        }
    }
    links
}
