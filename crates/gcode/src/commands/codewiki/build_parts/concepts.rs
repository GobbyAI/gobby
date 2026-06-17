use serde::Deserialize;

use super::super::*;
use super::curated_content::{self, CuratedPageKind};

const MAX_CONCEPT_MODULES: usize = 12;
const MAX_CONCEPT_LINKS: usize = 6;
/// Cap on the bounded "Explore" reference links a curated page emits (module
/// roots, not every member file). Replaces the old exhaustive
/// `## Reference Modules`/`## Source Files` dumps so the curated->reference
/// down-link surface (the missing_backlink source) collapses (root cause 6;
/// also the #853D mechanism).
const MAX_CURATED_KEY_COMPONENTS: usize = 8;
/// Cap on the bounded "Relevant source files" links a curated page lists (no
/// per-range expansion). Curated pages keep a small provenance footprint;
/// reference pages keep the full range-complete block.
const MAX_CURATED_SOURCE_FILE_LINKS: usize = 12;
/// Cap on *extra* model-supplied narrative chapters beyond the required
/// introduction/architecture/data-flow spine, so a verbose structure response
/// cannot crowd out the canonical guided tour.
const MAX_EXTRA_NARRATIVE_PAGES: usize = 4;

pub(crate) fn build_curated_navigation_docs(
    files: &[FileDoc],
    modules: &[ModuleDoc],
    leading_chunks: &std::collections::BTreeMap<String, LeadingChunk>,
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

    render_curated_navigation_docs(files, modules, plan, degraded, leading_chunks, generate)
}

#[allow(clippy::too_many_arguments)]
fn render_curated_navigation_docs(
    files: &[FileDoc],
    modules: &[ModuleDoc],
    plan: CuratedNavigationPlan,
    degraded: bool,
    leading_chunks: &std::collections::BTreeMap<String, LeadingChunk>,
    generate: &mut Option<&mut TextGenerator<'_>>,
) -> Vec<BuiltDoc> {
    let module_lookup = modules
        .iter()
        .map(|module| (module.module.as_str(), module))
        .collect::<std::collections::BTreeMap<_, _>>();
    let file_lookup = files
        .iter()
        .map(|file| (file.path.as_str(), file))
        .collect::<std::collections::BTreeMap<_, _>>();
    let mut concepts = normalize_concepts(plan.concept_modules, modules, files);
    let sections = normalize_sections(plan.sections, &concepts);
    let mut narrative_pages =
        normalize_narrative_pages(plan.narrative_pages, modules, files, &concepts);
    let all_spans = all_input_spans(files, modules);

    // Per-page content pass: expand each curated page's one-line summary into a
    // grounded, multi-section body (structural fallback on skip/failure). Runs
    // after normalization so member lists are final, and before rendering so
    // the bodies are in place. Mutates `concepts` first, so `concept_titles`
    // (which borrows it) is built afterwards.
    for concept in &mut concepts {
        let spans = item_spans(
            &concept.modules,
            &concept.files,
            &module_lookup,
            &file_lookup,
        );
        let result = curated_content::curated_page_body(
            CuratedPageKind::Concept,
            &concept.title,
            &concept.summary,
            &concept.modules,
            &concept.files,
            &module_lookup,
            &file_lookup,
            leading_chunks,
            &spans,
            generate,
        );
        concept.body = result.body;
        concept.body_degraded = result.degraded;
    }
    for page in &mut narrative_pages {
        let (member_modules, member_files) = narrative_members(page, &concepts);
        let spans = narrative_spans(page, &concepts, &module_lookup, &file_lookup);
        let result = curated_content::curated_page_body(
            CuratedPageKind::Narrative,
            &page.title,
            &page.summary,
            &member_modules,
            &member_files,
            &module_lookup,
            &file_lookup,
            leading_chunks,
            &spans,
            generate,
        );
        page.body = result.body;
        page.body_degraded = result.degraded;
    }

    let concept_titles = concepts
        .iter()
        .map(|concept| (concept.slug.as_str(), concept.title.as_str()))
        .collect::<std::collections::BTreeMap<_, _>>();

    let mut docs = Vec::new();
    docs.push(BuiltDoc {
        path: "code/concepts/index.md".to_string(),
        content: render_concept_tree(&sections, &concepts, &narrative_pages, &all_spans, degraded),
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
        let diagram_module = largest_member_module(&concept.modules, &module_lookup);
        docs.push(BuiltDoc {
            path: concept_doc_path(&concept.slug),
            content: render_concept_page(concept, &spans, degraded, diagram_module),
            degraded,
            summary: Some(concept.summary.clone()),
        });
    }

    for (index, page) in narrative_pages.iter().enumerate() {
        let spans = narrative_spans(page, &concepts, &module_lookup, &file_lookup);
        let (member_modules, _) = narrative_members(page, &concepts);
        let diagram_module = largest_member_module(&member_modules, &module_lookup);
        // Reciprocal neighbors in the ordered tour drive the Previous/Next nav.
        let prev = index
            .checked_sub(1)
            .map(|i| chapter_link(&narrative_pages[i]));
        let next = narrative_pages.get(index + 1).map(chapter_link);
        docs.push(BuiltDoc {
            path: narrative_doc_path(&page.slug),
            content: render_narrative_page(
                page,
                &spans,
                &concept_titles,
                degraded,
                diagram_module,
                prev,
                next,
            ),
            degraded,
            summary: Some(page.summary.clone()),
        });
    }

    docs
}

/// Borrows a narrative chapter's `(slug, title)` for guided-tour wikilinks.
fn chapter_link(page: &NarrativePage) -> (&str, &str) {
    (page.slug.as_str(), page.title.as_str())
}

fn render_concept_tree(
    sections: &[ConceptSection],
    concepts: &[ConceptModule],
    narrative_pages: &[NarrativePage],
    spans: &[SourceSpan],
    degraded: bool,
) -> String {
    let degraded_sources = degraded_sources(degraded);
    let mut doc = frontmatter_with_degradation_without_ranges(
        "Curated Concept Navigation",
        "code_concept_tree",
        spans,
        &degraded_sources,
    );
    append_curated_source_files(&mut doc, spans, MAX_CURATED_SOURCE_FILE_LINKS);
    doc.push_str("# Curated Concept Navigation\n\n");
    doc.push_str("Reader-first paths into the grounded code reference.\n\n");
    // The dependency-ordered narrative chapters are the primary entry point: a
    // numbered guided tour above the concept catalog.
    let chapters = narrative_pages.iter().map(chapter_link).collect::<Vec<_>>();
    curated_content::append_guided_tour(&mut doc, &chapters);
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
    doc
}

fn render_concept_page(
    concept: &ConceptModule,
    spans: &[SourceSpan],
    degraded: bool,
    diagram_module: Option<&ModuleDoc>,
) -> String {
    let degraded = degraded || concept.body_degraded;
    let degraded_sources = degraded_sources(degraded);
    let mut doc = frontmatter_with_degradation_without_ranges(
        &concept.title,
        "code_concept",
        spans,
        &degraded_sources,
    );
    append_curated_source_files(&mut doc, spans, MAX_CURATED_SOURCE_FILE_LINKS);
    let _ = std::fmt::Write::write_fmt(&mut doc, format_args!("# {}\n\n", concept.title));
    append_curated_body(
        &mut doc,
        concept.body.as_deref(),
        "Overview",
        &ground_text(&concept.summary, spans, None),
    );
    append_dependency_diagram(&mut doc, diagram_module);
    append_explore_section(&mut doc, &concept.modules, &concept.files);
    doc
}

fn render_narrative_page(
    page: &NarrativePage,
    spans: &[SourceSpan],
    concept_titles: &std::collections::BTreeMap<&str, &str>,
    degraded: bool,
    diagram_module: Option<&ModuleDoc>,
    prev: Option<(&str, &str)>,
    next: Option<(&str, &str)>,
) -> String {
    let degraded = degraded || page.body_degraded;
    let degraded_sources = degraded_sources(degraded);
    let mut doc = frontmatter_with_degradation_without_ranges(
        &page.title,
        "code_narrative",
        spans,
        &degraded_sources,
    );
    append_curated_source_files(&mut doc, spans, MAX_CURATED_SOURCE_FILE_LINKS);
    let _ = std::fmt::Write::write_fmt(&mut doc, format_args!("# {}\n\n", page.title));
    append_curated_body(
        &mut doc,
        page.body.as_deref(),
        "Guide",
        &ground_text(&page.summary, spans, None),
    );
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
    append_dependency_diagram(&mut doc, diagram_module);
    append_explore_section(&mut doc, &page.modules, &[]);
    curated_content::append_tour_nav(&mut doc, prev, next);
    doc
}

fn append_curated_body(
    doc: &mut String,
    body: Option<&str>,
    fallback_heading: &str,
    fallback_text: &str,
) {
    match body {
        Some(body) if !body.trim().is_empty() => {
            doc.push_str(body);
            if !body.ends_with('\n') {
                doc.push('\n');
            }
            doc.push('\n');
        }
        _ => write_section(doc, fallback_heading, fallback_text),
    }
}

/// Bounded reference links for a curated page: module roots (not every member
/// file), capped at [`MAX_CURATED_KEY_COMPONENTS`]. Files stay reachable and
/// reciprocal via their parent module pages and the reference appendix, so the
/// old exhaustive `## Reference Modules`/`## Source Files` down-link dumps -
/// the missing_backlink source - collapse.
fn append_explore_section(doc: &mut String, modules: &[String], files: &[String]) {
    let mut links: Vec<String> = modules
        .iter()
        .take(MAX_CURATED_KEY_COMPONENTS)
        .map(|module| module_wikilink(module))
        .collect();
    if links.is_empty() {
        links = files
            .iter()
            .take(MAX_CURATED_KEY_COMPONENTS)
            .map(|file| file_wikilink(file))
            .collect();
    }
    if links.is_empty() {
        return;
    }
    doc.push_str("## Explore\n\n");
    for link in links {
        let _ = std::fmt::Write::write_fmt(doc, format_args!("- {link}\n"));
    }
    doc.push('\n');
}

/// Union a narrative page's own modules/files with those of the concepts it
/// covers, so a chapter's content pass has real members to describe even when
/// the structure pass left its module/file lists sparse.
fn narrative_members(
    page: &NarrativePage,
    concepts: &[ConceptModule],
) -> (Vec<String>, Vec<String>) {
    let mut modules = page.modules.clone();
    let mut files = page.files.clone();
    for concept_slug in &page.concepts {
        if let Some(concept) = concepts
            .iter()
            .find(|concept| &concept.slug == concept_slug)
        {
            modules.extend(concept.modules.iter().cloned());
            files.extend(concept.files.iter().cloned());
        }
    }
    modules.sort();
    modules.dedup();
    files.sort();
    files.dedup();
    (modules, files)
}

/// The most substantial member module that already has a bounded dependency
/// diagram, ranked by file + submodule count then name (stable). Curated pages
/// reuse the precomputed `ModuleDoc.dependency_diagram` - no new graph work and
/// no fabricated edges.
fn largest_member_module<'a>(
    modules: &[String],
    module_lookup: &std::collections::BTreeMap<&str, &'a ModuleDoc>,
) -> Option<&'a ModuleDoc> {
    modules
        .iter()
        .filter_map(|module| module_lookup.get(module.as_str()).copied())
        .filter(|module| module.dependency_diagram.is_some())
        .max_by_key(|module| {
            (
                module.direct_files.len() + module.child_modules.len(),
                std::cmp::Reverse(module.module.clone()),
            )
        })
}

/// Inject a member module's already-bounded dependency diagram onto a curated
/// page. Honest about graph truncation (`graph-truncated` marker) and never
/// fabricates edges; emits nothing when the graph is unavailable or no diagram
/// was computed.
fn append_dependency_diagram(doc: &mut String, module: Option<&ModuleDoc>) {
    let Some(module) = module else {
        return;
    };
    let Some(diagram) = &module.dependency_diagram else {
        return;
    };
    if module.graph_availability == CodewikiGraphAvailability::Unavailable {
        return;
    }
    doc.push_str("## Architecture diagram\n\n");
    if module.graph_availability == CodewikiGraphAvailability::Truncated {
        doc.push_str("`degraded: graph-truncated`\n\n");
    }
    doc.push_str(diagram);
    doc.push('\n');
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
            body: None,
            body_degraded: false,
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
                body: None,
                body_degraded: false,
            },
            NarrativePage {
                slug: "architecture".to_string(),
                title: "Architecture".to_string(),
                summary: "Read across the major module boundaries and use the linked reference modules for grounded implementation detail.".to_string(),
                concepts: concept_titles.clone(),
                modules: root_modules,
                files: representative_files.clone(),
                body: None,
                body_degraded: false,
            },
            NarrativePage {
                slug: "data-flow".to_string(),
                title: "Data Flow".to_string(),
                summary: "Follow the representative files and modules that connect data entry, transformation, and output paths.".to_string(),
                concepts: concept_titles,
                modules: Vec::new(),
                files: representative_files,
                body: None,
                body_degraded: false,
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
    let normalized = pages
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

    // The canonical spine (introduction -> architecture -> data-flow) always
    // leads, in that dependency order; only *extra* model chapters are capped,
    // so a verbose structure response cannot crowd out the guided tour.
    const DEFAULT_CHAPTERS: [(&str, &str); 3] = [
        ("introduction", "Introduction"),
        ("architecture", "Architecture"),
        ("data-flow", "Data Flow"),
    ];
    let is_default = |slug: &str| DEFAULT_CHAPTERS.iter().any(|(default, _)| *default == slug);
    let mut spine: std::collections::BTreeMap<String, NarrativePage> =
        std::collections::BTreeMap::new();
    let mut extras: Vec<NarrativePage> = Vec::new();
    for page in normalized {
        if is_default(&page.slug) {
            spine.insert(page.slug.clone(), page);
        } else {
            extras.push(page);
        }
    }
    extras.truncate(MAX_EXTRA_NARRATIVE_PAGES);

    let mut ordered = Vec::with_capacity(DEFAULT_CHAPTERS.len() + extras.len());
    for (slug, title) in DEFAULT_CHAPTERS {
        if let Some(page) = spine.remove(slug) {
            ordered.push(page);
        } else {
            ordered.push(NarrativePage {
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
                body: None,
                body_degraded: false,
            });
        }
    }
    ordered.extend(extras);
    ordered
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
    /// Multi-section body from the per-page content pass. `#[serde(skip)]` so
    /// the structure-pass JSON parse ignores it (and extra model fields can't
    /// perturb deserialization); populated after normalization.
    #[serde(skip)]
    body: Option<String>,
    /// True when the content pass was attempted and fell back to the structural
    /// body, so the page records the degradation honestly (review #1).
    #[serde(skip)]
    body_degraded: bool,
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
    /// Multi-section chapter body from the per-page content pass; see
    /// [`ConceptModule::body`]. `#[serde(skip)]` for the same reason.
    #[serde(skip)]
    body: Option<String>,
    #[serde(skip)]
    body_degraded: bool,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn module_with_diagram(
        name: &str,
        diagram: Option<&str>,
        availability: CodewikiGraphAvailability,
    ) -> ModuleDoc {
        ModuleDoc {
            module: name.to_string(),
            summary: String::new(),
            source_spans: Vec::new(),
            direct_files: Vec::new(),
            child_modules: Vec::new(),
            component_ids: Vec::new(),
            dependency_diagram: diagram.map(str::to_string),
            call_diagram: None,
            graph_availability: availability,
            degraded: false,
            reused_page: None,
        }
    }

    #[test]
    fn curated_diagram_injected_when_graph_available() {
        let module = module_with_diagram(
            "src",
            Some("```mermaid\ngraph TD\n```"),
            CodewikiGraphAvailability::Available,
        );
        let mut doc = String::new();
        append_dependency_diagram(&mut doc, Some(&module));
        assert!(doc.contains("## Architecture diagram"), "{doc}");
        assert!(doc.contains("```mermaid"), "{doc}");
        assert!(!doc.contains("graph-truncated"), "{doc}");
    }

    #[test]
    fn curated_diagram_marks_truncated_graph() {
        let module = module_with_diagram(
            "src",
            Some("```mermaid\ngraph TD\n```"),
            CodewikiGraphAvailability::Truncated,
        );
        let mut doc = String::new();
        append_dependency_diagram(&mut doc, Some(&module));
        assert!(doc.contains("`degraded: graph-truncated`"), "{doc}");
        assert!(doc.contains("```mermaid"), "{doc}");
    }

    #[test]
    fn curated_diagram_skipped_when_unavailable_or_absent() {
        let mut doc = String::new();
        append_dependency_diagram(&mut doc, None);
        let unavailable = module_with_diagram(
            "src",
            Some("```mermaid\n```"),
            CodewikiGraphAvailability::Unavailable,
        );
        append_dependency_diagram(&mut doc, Some(&unavailable));
        let no_diagram = module_with_diagram("src", None, CodewikiGraphAvailability::Available);
        append_dependency_diagram(&mut doc, Some(&no_diagram));
        assert!(doc.is_empty(), "{doc}");
    }

    #[test]
    fn largest_member_module_requires_a_diagram() {
        let with = module_with_diagram(
            "src/a",
            Some("```mermaid\n```"),
            CodewikiGraphAvailability::Available,
        );
        let without = module_with_diagram("src/b", None, CodewikiGraphAvailability::Available);
        let lookup = std::collections::BTreeMap::from([("src/a", &with), ("src/b", &without)]);
        let members = ["src/a".to_string(), "src/b".to_string()];
        assert_eq!(
            largest_member_module(&members, &lookup).map(|module| module.module.as_str()),
            Some("src/a")
        );
        assert!(largest_member_module(&["src/b".to_string()], &lookup).is_none());
    }
}
