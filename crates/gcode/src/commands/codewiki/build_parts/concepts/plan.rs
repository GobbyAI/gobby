use super::super::super::*;
use super::support::{concept_title, slugify};
use super::types::*;
use super::{MAX_CONCEPT_LINKS, MAX_CONCEPT_MODULES, MAX_EXTRA_NARRATIVE_PAGES};

/// The most substantial member module that already has a bounded dependency
/// diagram, ranked by file + submodule count then name (stable). Curated pages
/// reuse the precomputed `ModuleDoc.dependency_diagram` - no new graph work and
/// no fabricated edges.
pub(super) fn largest_member_module<'a>(
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

pub(super) fn curated_navigation_prompt(files: &[FileDoc], modules: &[ModuleDoc]) -> String {
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

pub(super) fn parse_plan(raw: &str) -> Option<CuratedNavigationPlan> {
    let trimmed = raw.trim();
    let json = trimmed
        .strip_prefix("```json")
        .or_else(|| trimmed.strip_prefix("```"))
        .and_then(|body| body.strip_suffix("```"))
        .map(str::trim)
        .unwrap_or(trimmed);
    serde_json::from_str::<CuratedNavigationPlan>(json).ok()
}

pub(super) fn fallback_plan(files: &[FileDoc], modules: &[ModuleDoc]) -> CuratedNavigationPlan {
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

pub(super) fn normalize_concepts(
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

pub(super) fn normalize_sections(
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

pub(super) fn normalize_narrative_pages(
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
