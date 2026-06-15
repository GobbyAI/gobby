use std::path::{Path, PathBuf};

use crate::WikiError;
use crate::explainer::{CitationTarget, ExplainerGeneration, ExplainerReport, ground_explainer};

use super::paths::{
    ensure_synthesized_path_inside_vault, relative_path, slugify_unique, source_links,
    source_page_paths, wiki_link,
};
use super::render::{render_frontmatter, render_list_section, render_source_excerpts};
use super::types::{ArticleKind, SynthesisInput, SynthesizedPage};

pub fn synthesize_article(
    vault_root: &Path,
    input: &SynthesisInput,
    target_page: Option<PathBuf>,
    explainer: &ExplainerGeneration,
) -> Result<SynthesizedPage, WikiError> {
    let path = target_page.unwrap_or_else(|| {
        let directory = vault_root.join(input.target_kind.directory());
        let slug = slugify_unique(&input.topic, |slug| {
            directory.join(format!("{slug}.md")).exists()
        });
        directory.join(format!("{slug}.md"))
    });
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
        &input.topic,
        input.target_kind.source_kind(),
        &input.handoff_id,
        report.route.unwrap_or("fallback"),
        degraded_sources,
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
        ExplainerGeneration::Generated { body, model, route } => {
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
        let mut markdown = String::new();
        render_frontmatter(
            &mut markdown,
            &source.title,
            ArticleKind::Source.source_kind(),
            &input.handoff_id,
            "source",
            &[],
        );
        markdown.push_str("# ");
        markdown.push_str(&source.title);
        markdown.push_str("\n\n");
        markdown.push_str("Source path: `");
        markdown.push_str(&relative_path(vault_root, &source.path));
        markdown.push_str("`\n\n");
        render_list_section(&mut markdown, "Extracts", &source.chunks);
        render_list_section(
            &mut markdown,
            "Used by",
            std::slice::from_ref(&article_link),
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
