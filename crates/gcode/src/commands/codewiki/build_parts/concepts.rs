use serde::Deserialize;

use super::super::*;

const MAX_CONCEPT_MODULES: usize = 12;
const MAX_CONCEPT_LINKS: usize = 6;

pub(crate) fn build_curated_navigation_docs(
    files: &[FileDoc],
    modules: &[ModuleDoc],
    generate: &mut Option<&mut TextGenerator<'_>>,
    reuse: &mut Option<&mut ReusePlan>,
    progress: &mut CodewikiProgress,
) -> Vec<BuiltDoc> {
    let all_spans = all_input_spans(files, modules);
    let all_sources = span_files(&all_spans);
    if let Some(reused_docs) = reuse.as_deref_mut().and_then(|plan| {
        plan.reusable_page("code/concepts/index.md", &all_sources)?;
        plan.reusable_pages_with_prefixes(&["code/concepts/", "code/narrative/"])
    }) {
        progress.emit("reusing curated navigation docs (sources unchanged)");
        return reused_docs;
    }

    progress.emit("generating curated navigation docs");
    let mut degraded = false;
    let plan = match maybe_generate(
        generate,
        &curated_navigation_prompt(files, modules),
        prompts::CURATED_NAVIGATION_SYSTEM,
        PromptTier::Aggregate,
    ) {
        Generation::Generated(generated) => match parse_plan(&generated) {
            Some(plan) => plan,
            None => {
                degraded = true;
                fallback_plan(files, modules)
            }
        },
        Generation::Failed => {
            degraded = true;
            fallback_plan(files, modules)
        }
        Generation::Skipped => fallback_plan(files, modules),
    };

    render_curated_navigation_docs(files, modules, plan, degraded)
}

fn render_curated_navigation_docs(
    files: &[FileDoc],
    modules: &[ModuleDoc],
    plan: CuratedNavigationPlan,
    degraded: bool,
) -> Vec<BuiltDoc> {
    let module_lookup = modules
        .iter()
        .map(|module| (module.module.as_str(), module))
        .collect::<std::collections::BTreeMap<_, _>>();
    let file_lookup = files
        .iter()
        .map(|file| (file.path.as_str(), file))
        .collect::<std::collections::BTreeMap<_, _>>();
    let concepts = normalize_concepts(plan.concept_modules, modules, files);
    let sections = normalize_sections(plan.sections, &concepts);
    let narrative_pages =
        normalize_narrative_pages(plan.narrative_pages, modules, files, &concepts);
    let all_spans = all_input_spans(files, modules);
    let concept_titles = concepts
        .iter()
        .map(|concept| (concept.slug.as_str(), concept.title.as_str()))
        .collect::<std::collections::BTreeMap<_, _>>();

    let mut docs = Vec::new();
    docs.push(BuiltDoc {
        path: "code/concepts/index.md".to_string(),
        content: render_concept_tree(&sections, &concepts, &all_spans, degraded),
        degraded,
        summary: Some("Curated concept navigation over the code reference.".to_string()),
    });

    for concept in &concepts {
        let spans = item_spans(
            &concept.modules,
            &concept.files,
            &module_lookup,
            &file_lookup,
        );
        docs.push(BuiltDoc {
            path: concept_doc_path(&concept.slug),
            content: render_concept_page(concept, &spans, degraded),
            degraded,
            summary: Some(concept.summary.clone()),
        });
    }

    for page in &narrative_pages {
        let spans = narrative_spans(page, &concepts, &module_lookup, &file_lookup);
        docs.push(BuiltDoc {
            path: narrative_doc_path(&page.slug),
            content: render_narrative_page(page, &spans, &concept_titles, degraded),
            degraded,
            summary: Some(page.summary.clone()),
        });
    }

    docs
}

fn render_concept_tree(
    sections: &[ConceptSection],
    concepts: &[ConceptModule],
    spans: &[SourceSpan],
    degraded: bool,
) -> String {
    let degraded_sources = degraded_sources(degraded);
    let mut doc = frontmatter_with_degradation(
        "Curated Concept Navigation",
        "code_concept_tree",
        spans,
        &degraded_sources,
    );
    append_relevant_source_files(&mut doc, spans);
    doc.push_str("# Curated Concept Navigation\n\n");
    doc.push_str("Reader-first paths into the grounded code reference.\n\n");
    doc.push_str("## Concept Tree\n\n");
    for section in sections {
        let _ = std::fmt::Write::write_fmt(&mut doc, format_args!("### {}\n\n", section.title));
        if !section.summary.trim().is_empty() {
            let _ = std::fmt::Write::write_fmt(&mut doc, format_args!("{}\n\n", section.summary));
        }
        for concept_slug in &section.concepts {
            if let Some(concept) = concepts
                .iter()
                .find(|concept| &concept.slug == concept_slug)
            {
                let _ = std::fmt::Write::write_fmt(
                    &mut doc,
                    format_args!(
                        "- [[{}|{}]] - {}\n",
                        concept_doc_stem(&concept.slug),
                        concept.title,
                        concept.summary
                    ),
                );
            }
        }
        doc.push('\n');
    }
    doc.push_str("## Narrative Tours\n\n");
    doc.push_str("- [[code/narrative/introduction|Introduction]]\n");
    doc.push_str("- [[code/narrative/architecture|Architecture]]\n");
    doc.push_str("- [[code/narrative/data-flow|Data Flow]]\n");
    doc
}

fn render_concept_page(concept: &ConceptModule, spans: &[SourceSpan], degraded: bool) -> String {
    let degraded_sources = degraded_sources(degraded);
    let mut doc =
        frontmatter_with_degradation(&concept.title, "code_concept", spans, &degraded_sources);
    append_relevant_source_files(&mut doc, spans);
    let _ = std::fmt::Write::write_fmt(&mut doc, format_args!("# {}\n\n", concept.title));
    write_section(
        &mut doc,
        "Overview",
        &ground_text(&concept.summary, spans, None),
    );
    if !concept.modules.is_empty() {
        doc.push_str("## Reference Modules\n\n");
        for module in &concept.modules {
            let _ = std::fmt::Write::write_fmt(
                &mut doc,
                format_args!("- {}\n", module_wikilink(module)),
            );
        }
        doc.push('\n');
    }
    if !concept.files.is_empty() {
        doc.push_str("## Source Files\n\n");
        for file in &concept.files {
            let _ =
                std::fmt::Write::write_fmt(&mut doc, format_args!("- {}\n", file_wikilink(file)));
        }
        doc.push('\n');
    }
    doc
}

fn render_narrative_page(
    page: &NarrativePage,
    spans: &[SourceSpan],
    concept_titles: &std::collections::BTreeMap<&str, &str>,
    degraded: bool,
) -> String {
    let degraded_sources = degraded_sources(degraded);
    let mut doc =
        frontmatter_with_degradation(&page.title, "code_narrative", spans, &degraded_sources);
    append_relevant_source_files(&mut doc, spans);
    let _ = std::fmt::Write::write_fmt(&mut doc, format_args!("# {}\n\n", page.title));
    write_section(&mut doc, "Guide", &ground_text(&page.summary, spans, None));
    if !page.concepts.is_empty() {
        doc.push_str("## Concepts\n\n");
        for concept in &page.concepts {
            let title = concept_titles
                .get(concept.as_str())
                .copied()
                .unwrap_or(concept);
            let _ = std::fmt::Write::write_fmt(
                &mut doc,
                format_args!("- [[{}|{}]]\n", concept_doc_stem(concept), title),
            );
        }
        doc.push('\n');
    }
    if !page.modules.is_empty() {
        doc.push_str("## Reference Modules\n\n");
        for module in &page.modules {
            let _ = std::fmt::Write::write_fmt(
                &mut doc,
                format_args!("- {}\n", module_wikilink(module)),
            );
        }
        doc.push('\n');
    }
    if !page.files.is_empty() {
        doc.push_str("## Source Files\n\n");
        for file in &page.files {
            let _ =
                std::fmt::Write::write_fmt(&mut doc, format_args!("- {}\n", file_wikilink(file)));
        }
        doc.push('\n');
    }
    doc
}

fn curated_navigation_prompt(files: &[FileDoc], modules: &[ModuleDoc]) -> String {
    let mut prompt = String::from(
        "Build a curated codewiki navigation layer over the existing grounded reference.\n\
         Return only JSON with keys concept_modules, sections, and narrative_pages.\n\
         Each concept module must name a user-facing concept and link to existing module/file names.\n\
         Do not duplicate source content.\n\n\
         Schema:\n\
         {\"concept_modules\":[{\"title\":\"...\",\"summary\":\"...\",\"modules\":[\"...\"],\"files\":[\"...\"]}],\
         \"sections\":[{\"title\":\"...\",\"summary\":\"...\",\"concepts\":[\"concept title\"]}],\
         \"narrative_pages\":[{\"slug\":\"introduction\",\"title\":\"...\",\"summary\":\"...\",\"concepts\":[\"concept title\"],\"modules\":[\"...\"],\"files\":[\"...\"]}]}\n\n\
         Available modules:\n",
    );
    for module in modules.iter().take(40) {
        let _ = std::fmt::Write::write_fmt(
            &mut prompt,
            format_args!(
                "- {}: {} ({} direct files, {} child modules)\n",
                module.module,
                module.summary,
                module.direct_files.len(),
                module.child_modules.len()
            ),
        );
    }
    prompt.push_str("\nRepresentative files:\n");
    for file in files.iter().take(80) {
        let _ = std::fmt::Write::write_fmt(
            &mut prompt,
            format_args!("- {}: {} [{}]\n", file.path, file.summary, file.module),
        );
    }
    prompt
}

fn parse_plan(raw: &str) -> Option<CuratedNavigationPlan> {
    let trimmed = raw.trim();
    let json = trimmed
        .strip_prefix("```json")
        .or_else(|| trimmed.strip_prefix("```"))
        .and_then(|body| body.strip_suffix("```"))
        .map(str::trim)
        .unwrap_or(trimmed);
    serde_json::from_str::<CuratedNavigationPlan>(json).ok()
}

fn fallback_plan(files: &[FileDoc], modules: &[ModuleDoc]) -> CuratedNavigationPlan {
    let concepts = modules
        .iter()
        .filter(|module| !module.direct_files.is_empty() || !module.child_modules.is_empty())
        .take(MAX_CONCEPT_MODULES)
        .map(|module| ConceptModule {
            slug: slugify(&module.module),
            title: concept_title(&module.module),
            summary: module.summary.clone(),
            modules: vec![module.module.clone()],
            files: module
                .direct_files
                .iter()
                .take(MAX_CONCEPT_LINKS)
                .map(|file| file.path.clone())
                .collect(),
        })
        .collect::<Vec<_>>();

    let section = ConceptSection {
        title: "System Tour".to_string(),
        summary: "A curated path through the primary modules.".to_string(),
        concepts: concepts
            .iter()
            .map(|concept| concept.title.clone())
            .collect(),
    };

    let root_modules = modules
        .iter()
        .filter(|module| parent_module(&module.module).is_none())
        .map(|module| module.module.clone())
        .take(MAX_CONCEPT_LINKS)
        .collect::<Vec<_>>();
    let representative_files = files
        .iter()
        .take(MAX_CONCEPT_LINKS)
        .map(|file| file.path.clone())
        .collect::<Vec<_>>();
    let concept_titles = concepts
        .iter()
        .take(MAX_CONCEPT_LINKS)
        .map(|concept| concept.title.clone())
        .collect::<Vec<_>>();

    CuratedNavigationPlan {
        concept_modules: concepts,
        sections: vec![section],
        narrative_pages: vec![
            NarrativePage {
                slug: "introduction".to_string(),
                title: "Introduction".to_string(),
                summary: "Start with the highest-level modules, then follow the concept pages into source-backed reference pages.".to_string(),
                concepts: concept_titles.clone(),
                modules: root_modules.clone(),
                files: representative_files.clone(),
            },
            NarrativePage {
                slug: "architecture".to_string(),
                title: "Architecture".to_string(),
                summary: "Read across the major module boundaries and use the linked reference modules for grounded implementation detail.".to_string(),
                concepts: concept_titles.clone(),
                modules: root_modules,
                files: representative_files.clone(),
            },
            NarrativePage {
                slug: "data-flow".to_string(),
                title: "Data Flow".to_string(),
                summary: "Follow the representative files and modules that connect data entry, transformation, and output paths.".to_string(),
                concepts: concept_titles,
                modules: Vec::new(),
                files: representative_files,
            },
        ],
    }
}

fn normalize_concepts(
    concepts: Vec<ConceptModule>,
    modules: &[ModuleDoc],
    files: &[FileDoc],
) -> Vec<ConceptModule> {
    let known_modules = modules
        .iter()
        .map(|module| module.module.as_str())
        .collect::<std::collections::BTreeSet<_>>();
    let known_files = files
        .iter()
        .map(|file| file.path.as_str())
        .collect::<std::collections::BTreeSet<_>>();

    concepts
        .into_iter()
        .filter_map(|mut concept| {
            concept.title = concept.title.trim().to_string();
            concept.summary = concept.summary.trim().to_string();
            concept.slug = slugify(if concept.slug.trim().is_empty() {
                &concept.title
            } else {
                &concept.slug
            });
            concept
                .modules
                .retain(|module| known_modules.contains(module.as_str()));
            concept
                .files
                .retain(|file| known_files.contains(file.as_str()));
            concept.modules.truncate(MAX_CONCEPT_LINKS);
            concept.files.truncate(MAX_CONCEPT_LINKS);
            if concept.title.is_empty() || (concept.modules.is_empty() && concept.files.is_empty())
            {
                None
            } else {
                Some(concept)
            }
        })
        .take(MAX_CONCEPT_MODULES)
        .collect()
}

fn normalize_sections(
    sections: Vec<ConceptSection>,
    concepts: &[ConceptModule],
) -> Vec<ConceptSection> {
    let concept_by_key = concept_key_map(concepts);
    let normalized = sections
        .into_iter()
        .filter_map(|mut section| {
            section.title = section.title.trim().to_string();
            section.summary = section.summary.trim().to_string();
            section.concepts = section
                .concepts
                .into_iter()
                .filter_map(|concept| concept_by_key.get(concept.trim()).cloned())
                .collect();
            if section.title.is_empty() || section.concepts.is_empty() {
                None
            } else {
                Some(section)
            }
        })
        .collect::<Vec<_>>();
    if normalized.is_empty() && !concepts.is_empty() {
        vec![ConceptSection {
            title: "System Tour".to_string(),
            summary: "A curated path through the primary concepts.".to_string(),
            concepts: concepts
                .iter()
                .map(|concept| concept.slug.clone())
                .collect(),
        }]
    } else {
        normalized
    }
}

fn normalize_narrative_pages(
    pages: Vec<NarrativePage>,
    modules: &[ModuleDoc],
    files: &[FileDoc],
    concepts: &[ConceptModule],
) -> Vec<NarrativePage> {
    let concept_by_key = concept_key_map(concepts);
    let known_modules = modules
        .iter()
        .map(|module| module.module.as_str())
        .collect::<std::collections::BTreeSet<_>>();
    let known_files = files
        .iter()
        .map(|file| file.path.as_str())
        .collect::<std::collections::BTreeSet<_>>();
    let mut normalized = pages
        .into_iter()
        .filter_map(|mut page| {
            page.title = page.title.trim().to_string();
            page.summary = page.summary.trim().to_string();
            page.slug = slugify(if page.slug.trim().is_empty() {
                &page.title
            } else {
                &page.slug
            });
            page.concepts = page
                .concepts
                .into_iter()
                .filter_map(|concept| concept_by_key.get(concept.trim()).cloned())
                .collect();
            page.modules
                .retain(|module| known_modules.contains(module.as_str()));
            page.files
                .retain(|file| known_files.contains(file.as_str()));
            if page.title.is_empty() || page.summary.is_empty() {
                None
            } else {
                Some(page)
            }
        })
        .collect::<Vec<_>>();

    for (slug, title) in [
        ("introduction", "Introduction"),
        ("architecture", "Architecture"),
        ("data-flow", "Data Flow"),
    ] {
        if !normalized.iter().any(|page| page.slug == slug) {
            normalized.push(NarrativePage {
                slug: slug.to_string(),
                title: title.to_string(),
                summary: format!("{title} tour linking into the code reference."),
                concepts: concepts
                    .iter()
                    .take(MAX_CONCEPT_LINKS)
                    .map(|concept| concept.slug.clone())
                    .collect(),
                modules: Vec::new(),
                files: Vec::new(),
            });
        }
    }
    normalized
}

fn concept_key_map(concepts: &[ConceptModule]) -> std::collections::BTreeMap<&str, String> {
    let mut map = std::collections::BTreeMap::new();
    for concept in concepts {
        map.insert(concept.title.as_str(), concept.slug.clone());
        map.insert(concept.slug.as_str(), concept.slug.clone());
    }
    map
}

fn all_input_spans(files: &[FileDoc], modules: &[ModuleDoc]) -> Vec<SourceSpan> {
    let mut spans = std::collections::BTreeSet::new();
    for file in files {
        spans.extend(file.source_spans.iter().cloned());
    }
    for module in modules {
        spans.extend(module.source_spans.iter().cloned());
    }
    spans.into_iter().collect()
}

fn narrative_spans(
    page: &NarrativePage,
    concepts: &[ConceptModule],
    module_lookup: &std::collections::BTreeMap<&str, &ModuleDoc>,
    file_lookup: &std::collections::BTreeMap<&str, &FileDoc>,
) -> Vec<SourceSpan> {
    let mut spans = std::collections::BTreeSet::new();
    spans.extend(item_spans(
        &page.modules,
        &page.files,
        module_lookup,
        file_lookup,
    ));
    for concept_slug in &page.concepts {
        if let Some(concept) = concepts
            .iter()
            .find(|concept| &concept.slug == concept_slug)
        {
            spans.extend(item_spans(
                &concept.modules,
                &concept.files,
                module_lookup,
                file_lookup,
            ));
        }
    }
    spans.into_iter().collect()
}

fn item_spans(
    modules: &[String],
    files: &[String],
    module_lookup: &std::collections::BTreeMap<&str, &ModuleDoc>,
    file_lookup: &std::collections::BTreeMap<&str, &FileDoc>,
) -> Vec<SourceSpan> {
    let mut spans = std::collections::BTreeSet::new();
    for module in modules {
        if let Some(module) = module_lookup.get(module.as_str()) {
            spans.extend(module.source_spans.iter().cloned());
        }
    }
    for file in files {
        if let Some(file) = file_lookup.get(file.as_str()) {
            spans.extend(file.source_spans.iter().cloned());
        }
    }
    spans.into_iter().collect()
}

fn degraded_sources(degraded: bool) -> Vec<String> {
    if degraded {
        vec!["model-unavailable".to_string()]
    } else {
        Vec::new()
    }
}

fn concept_title(module: &str) -> String {
    module
        .rsplit('/')
        .next()
        .unwrap_or(module)
        .split(['_', '-'])
        .filter(|part| !part.is_empty())
        .map(|part| {
            let mut chars = part.chars();
            match chars.next() {
                Some(first) => first.to_uppercase().chain(chars).collect::<String>(),
                None => String::new(),
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}

fn concept_doc_path(slug: &str) -> String {
    format!("{}.md", concept_doc_stem(slug))
}

fn concept_doc_stem(slug: &str) -> String {
    format!("code/concepts/{slug}")
}

fn narrative_doc_path(slug: &str) -> String {
    format!("code/narrative/{slug}.md")
}

fn slugify(value: &str) -> String {
    let mut slug = String::new();
    let mut previous_dash = false;
    for raw in value.chars() {
        let ch = raw.to_ascii_lowercase();
        if ch.is_ascii_alphanumeric() {
            slug.push(ch);
            previous_dash = false;
        } else if !previous_dash && !slug.is_empty() {
            slug.push('-');
            previous_dash = true;
        }
    }
    slug.trim_matches('-').to_string()
}

#[derive(Debug, Deserialize)]
struct CuratedNavigationPlan {
    #[serde(default)]
    concept_modules: Vec<ConceptModule>,
    #[serde(default)]
    sections: Vec<ConceptSection>,
    #[serde(default)]
    narrative_pages: Vec<NarrativePage>,
}

#[derive(Debug, Deserialize)]
struct ConceptModule {
    #[serde(default)]
    slug: String,
    title: String,
    #[serde(default)]
    summary: String,
    #[serde(default)]
    modules: Vec<String>,
    #[serde(default)]
    files: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct ConceptSection {
    title: String,
    #[serde(default)]
    summary: String,
    #[serde(default)]
    concepts: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct NarrativePage {
    #[serde(default)]
    slug: String,
    title: String,
    #[serde(default)]
    summary: String,
    #[serde(default)]
    concepts: Vec<String>,
    #[serde(default)]
    modules: Vec<String>,
    #[serde(default)]
    files: Vec<String>,
}
