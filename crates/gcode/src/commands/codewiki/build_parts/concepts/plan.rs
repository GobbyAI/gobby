use super::super::super::*;
use super::support::{concept_title, slugify};
use super::types::*;
use super::{MAX_CONCEPT_LINKS, MAX_CONCEPT_MODULES, MAX_EXTRA_NARRATIVE_PAGES};

#[derive(Clone, Copy)]
struct DefaultChapter {
    slug: &'static str,
    title: &'static str,
    summary: &'static str,
    patterns: &'static [&'static str],
}

const DEFAULT_CHAPTERS: [DefaultChapter; 10] = [
    DefaultChapter {
        slug: "introduction",
        title: "Introduction",
        summary: "Start with the system purpose, major crates, runtime modes, and the shortest path into the grounded reference.",
        patterns: &[],
    },
    DefaultChapter {
        slug: "architecture",
        title: "Architecture",
        summary: "Explain the codebase layers, ownership boundaries, and why the modules are separated the way they are.",
        patterns: &[
            "architecture",
            "config",
            "context",
            "schema",
            "model",
            "core",
        ],
    },
    DefaultChapter {
        slug: "indexing-pipeline",
        title: "Indexing Pipeline",
        summary: "Follow file discovery, parsing, chunking, symbol extraction, hashing, and incremental index updates.",
        patterns: &["index", "parser", "walker", "chunk", "language", "hash"],
    },
    DefaultChapter {
        slug: "search-rrf",
        title: "Search/RRF",
        summary: "Trace lexical, semantic, graph-boosted, and reciprocal-rank-fusion search behavior.",
        patterns: &["search", "rrf", "bm25", "semantic", "rank", "query"],
    },
    DefaultChapter {
        slug: "codewiki-generation",
        title: "CodeWiki Generation",
        summary: "Explain how gcode builds grounded wiki pages, prompts AI sections, verifies claims, and writes cache metadata.",
        patterns: &[
            "codewiki",
            "wiki",
            "prompt",
            "verify",
            "frontmatter",
            "reuse",
        ],
    },
    DefaultChapter {
        slug: "gwiki-vault",
        title: "Gwiki Vault",
        summary: "Describe vault layout, Obsidian-compatible pages, ask/search entrypoints, and project-scoped wiki operations.",
        patterns: &["gwiki", "vault", "obsidian", "librarian", "scope", "wiki"],
    },
    DefaultChapter {
        slug: "graph-vector-storage",
        title: "Graph/Vector Storage",
        summary: "Map PostgreSQL hub data, FalkorDB graph projection, Qdrant vectors, and degraded behavior when services are absent.",
        patterns: &[
            "graph",
            "falkor",
            "qdrant",
            "vector",
            "embedding",
            "postgres",
        ],
    },
    DefaultChapter {
        slug: "cli-contracts",
        title: "CLI Contracts",
        summary: "Cover command dispatch, contract catalogs, output modes, and the flags users and agents rely on.",
        patterns: &["cli", "command", "contract", "dispatch", "main", "output"],
    },
    DefaultChapter {
        slug: "failure-modes",
        title: "Failure Modes",
        summary: "Surface fallback paths, degraded service states, unavailable dependencies, and verification notes.",
        patterns: &[
            "degrad",
            "fallback",
            "unavailable",
            "failure",
            "error",
            "verify",
        ],
    },
    DefaultChapter {
        slug: "contributor-guide",
        title: "Contributor Guide",
        summary: "Explain how to make changes safely, run focused validation, respect ownership boundaries, and regenerate the wiki.",
        patterns: &[
            "test",
            "setup",
            "contribut",
            "workflow",
            "validation",
            "release",
        ],
    },
];

pub(crate) fn default_chapter_links() -> Vec<(String, &'static str)> {
    DEFAULT_CHAPTERS
        .iter()
        .enumerate()
        .map(|(index, chapter)| (ordinal_narrative_slug(index, chapter.title), chapter.title))
        .collect()
}

fn ordinal_narrative_slug(index: usize, title: &str) -> String {
    let ordinal = index + 1;
    let title_slug = slugify(title);
    if title_slug.is_empty() {
        format!("{ordinal:02}")
    } else {
        format!("{ordinal:02}-{title_slug}")
    }
}

pub(super) fn curated_navigation_prompt(files: &[FileDoc], modules: &[ModuleDoc]) -> String {
    let mut prompt = String::from(
        "Build a curated handbook layer over the existing grounded reference.\n\
         Return only JSON with keys concept_modules, sections, and narrative_pages.\n\
         Each concept module must name a user-facing concept and link to existing module/file names.\n\
         Do not duplicate source content.\n\
         Prefer 8-12 narrative_pages and include these handbook chapters when the supplied modules/files support them: \
         Introduction, Architecture, Indexing Pipeline, Search/RRF, CodeWiki Generation, Gwiki Vault, Graph/Vector Storage, CLI Contracts, Failure Modes, Contributor Guide.\n\n\
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
            body_degraded_sources: Vec::new(),
            verify_notes: Vec::new(),
            body_observability: GenerationObservability::default(),
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
        narrative_pages: default_chapter_pages(
            &concept_titles,
            &root_modules,
            &representative_files,
        ),
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

    let mut concepts = concepts
        .into_iter()
        .filter_map(|mut concept| {
            concept.title = concept.title.trim().to_string();
            concept.summary = concept.summary.trim().to_string();
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
                return None;
            }
            // Stable, churn-free slug (#900): derive the slug from the concept's
            // members, not the volatile AI title. The lexicographically smallest
            // member module/file is a deterministic identity that survives the
            // planner re-titling or re-grouping this concept, so cross-page links
            // to it stay valid across regens instead of dangling when the title
            // changes. (Mirrors the module-derived slug the fallback plan uses.)
            concept.slug = concept
                .modules
                .iter()
                .chain(concept.files.iter())
                .min()
                .map(|member| slugify(member))
                .unwrap_or_else(|| slugify(&concept.title));
            Some(concept)
        })
        .take(MAX_CONCEPT_MODULES)
        .collect::<Vec<_>>();

    // Deterministic de-duplication: if two concepts resolve to the same member
    // slug, later ones (in plan order) take a stable numeric suffix so a link
    // never resolves to the wrong concept page.
    let mut seen = std::collections::BTreeMap::<String, usize>::new();
    for concept in &mut concepts {
        let base_slug = concept.slug.clone();
        let mut count = *seen.get(&base_slug).unwrap_or(&0);
        let mut final_slug = base_slug.clone();
        if count > 0 {
            loop {
                final_slug = format!("{}-{}", base_slug, count + 1);
                if !seen.contains_key(&final_slug) {
                    break;
                }
                count += 1;
            }
            concept.slug = final_slug.clone();
        }
        seen.insert(base_slug.clone(), count + 1);
        if final_slug != base_slug {
            seen.insert(final_slug, 1);
        }
    }
    concepts
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
                page.title.as_str()
            } else {
                strip_ordinal_slug_prefix(page.slug.trim())
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

    // The canonical handbook spine always leads, in dependency order; only
    // *extra* model chapters are capped, so a verbose structure response cannot
    // crowd out the guided tour.
    let is_default = |slug: &str| DEFAULT_CHAPTERS.iter().any(|chapter| chapter.slug == slug);
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
    for chapter in DEFAULT_CHAPTERS {
        if let Some(mut page) = spine.remove(chapter.slug) {
            enrich_default_chapter(&mut page, chapter, modules, files, concepts);
            ordered.push(page);
        } else {
            ordered.push(default_chapter_page(
                chapter,
                chapter_concepts(chapter, concepts),
                chapter_modules(chapter, modules),
                chapter_files(chapter, files),
            ));
        }
    }
    ordered.extend(extras);
    // Deterministic, reading-ordered slugs (#900). The in-memory tour order is
    // the canonical sequence, so pin each chapter's on-disk slug to its 1-based
    // position. This sorts the narrative folder in tour order instead of
    // alphabetically and — paired with the orphan GC in `DocSink::finish` —
    // reclaims a churned chapter's old page instead of leaving a broken-link /
    // degraded orphan.
    //
    // Every chapter gets a readable, position-ordered slug `NN-<title>`
    // (`01-introduction`, `04-getting-started-setup-configuration`, ...). The
    // ordinal prefix pins tour order on disk and keeps slugs unique across
    // chapters. The fixed spine titles never churn; an *extra* chapter's title
    // is volatile, so a re-title moves it to a new `NN-<title>` page — but the
    // orphan GC in `DocSink::finish` reclaims the old one, and the curated layer
    // (the only thing that links extras) is regenerated in the same run, so no
    // cross-page link dangles. A title that slugifies to nothing falls back to
    // the bare ordinal so a page is never named `NN-`.
    for (index, page) in ordered.iter_mut().enumerate() {
        page.slug = ordinal_narrative_slug(index, &page.title);
    }
    ordered
}

fn default_chapter_pages(
    concepts: &[String],
    modules: &[String],
    files: &[String],
) -> Vec<NarrativePage> {
    DEFAULT_CHAPTERS
        .iter()
        .copied()
        .map(|chapter| {
            default_chapter_page(chapter, concepts.to_vec(), modules.to_vec(), files.to_vec())
        })
        .collect()
}

fn default_chapter_page(
    chapter: DefaultChapter,
    concepts: Vec<String>,
    modules: Vec<String>,
    files: Vec<String>,
) -> NarrativePage {
    NarrativePage {
        slug: chapter.slug.to_string(),
        title: chapter.title.to_string(),
        summary: chapter.summary.to_string(),
        concepts,
        modules,
        files,
        body: None,
        body_degraded_sources: Vec::new(),
        verify_notes: Vec::new(),
        body_observability: GenerationObservability::default(),
    }
}

fn enrich_default_chapter(
    page: &mut NarrativePage,
    chapter: DefaultChapter,
    modules: &[ModuleDoc],
    files: &[FileDoc],
    concepts: &[ConceptModule],
) {
    if page.concepts.is_empty() {
        page.concepts = chapter_concepts(chapter, concepts);
    }
    if page.modules.is_empty() {
        page.modules = chapter_modules(chapter, modules);
    }
    if page.files.is_empty() {
        page.files = chapter_files(chapter, files);
    }
}

fn chapter_concepts(chapter: DefaultChapter, concepts: &[ConceptModule]) -> Vec<String> {
    if chapter.patterns.is_empty() {
        return concepts
            .iter()
            .take(MAX_CONCEPT_LINKS)
            .map(|concept| concept.slug.clone())
            .collect();
    }
    let matched = concepts
        .iter()
        .filter(|concept| {
            matches_chapter_text(chapter, &concept.slug)
                || matches_chapter_text(chapter, &concept.title)
                || matches_chapter_text(chapter, &concept.summary)
                || concept
                    .modules
                    .iter()
                    .any(|module| matches_chapter_text(chapter, module))
                || concept
                    .files
                    .iter()
                    .any(|file| matches_chapter_text(chapter, file))
        })
        .take(MAX_CONCEPT_LINKS)
        .map(|concept| concept.slug.clone())
        .collect::<Vec<_>>();
    if matched.is_empty() {
        concepts
            .iter()
            .take(MAX_CONCEPT_LINKS)
            .map(|concept| concept.slug.clone())
            .collect()
    } else {
        matched
    }
}

fn chapter_modules(chapter: DefaultChapter, modules: &[ModuleDoc]) -> Vec<String> {
    if chapter.patterns.is_empty() {
        return root_module_names(modules);
    }
    let matched = modules
        .iter()
        .filter(|module| {
            matches_chapter_text(chapter, &module.module)
                || matches_chapter_text(chapter, &module.summary)
        })
        .take(MAX_CONCEPT_LINKS)
        .map(|module| module.module.clone())
        .collect::<Vec<_>>();
    if matched.is_empty() {
        root_module_names(modules)
    } else {
        matched
    }
}

fn chapter_files(chapter: DefaultChapter, files: &[FileDoc]) -> Vec<String> {
    if chapter.patterns.is_empty() {
        return representative_file_names(files);
    }
    let matched = files
        .iter()
        .filter(|file| {
            matches_chapter_text(chapter, &file.path)
                || matches_chapter_text(chapter, &file.summary)
                || matches_chapter_text(chapter, &file.module)
        })
        .take(MAX_CONCEPT_LINKS)
        .map(|file| file.path.clone())
        .collect::<Vec<_>>();
    if matched.is_empty() {
        representative_file_names(files)
    } else {
        matched
    }
}

fn root_module_names(modules: &[ModuleDoc]) -> Vec<String> {
    modules
        .iter()
        .filter(|module| parent_module(&module.module).is_none())
        .map(|module| module.module.clone())
        .take(MAX_CONCEPT_LINKS)
        .collect()
}

fn representative_file_names(files: &[FileDoc]) -> Vec<String> {
    files
        .iter()
        .take(MAX_CONCEPT_LINKS)
        .map(|file| file.path.clone())
        .collect()
}

fn matches_chapter_text(chapter: DefaultChapter, text: &str) -> bool {
    let text = text.to_ascii_lowercase();
    chapter
        .patterns
        .iter()
        .any(|pattern| text.contains(pattern))
}

fn strip_ordinal_slug_prefix(slug: &str) -> &str {
    let Some((prefix, suffix)) = slug.split_once('-') else {
        return slug;
    };
    if prefix.len() == 2 && prefix.chars().all(|c| c.is_ascii_digit()) && !suffix.is_empty() {
        suffix
    } else {
        slug
    }
}

fn concept_key_map(concepts: &[ConceptModule]) -> std::collections::BTreeMap<String, String> {
    let mut map = std::collections::BTreeMap::new();
    for concept in concepts {
        // Map every identifier the planner might use to reference a concept in a
        // narrative page to its final member-derived slug: the title, the
        // title-derived slug (the planner's previous default identifier, still
        // emitted by some plans), and the final slug itself. Keeps narrative ->
        // concept links resolving now that the slug no longer comes from the
        // title (#900).
        map.insert(concept.title.clone(), concept.slug.clone());
        map.insert(slugify(&concept.title), concept.slug.clone());
        map.insert(concept.slug.clone(), concept.slug.clone());
    }
    map
}

#[cfg(test)]
mod tests {
    use super::*;

    fn narrative_page(slug: &str, title: &str) -> NarrativePage {
        NarrativePage {
            slug: slug.to_string(),
            title: title.to_string(),
            summary: "Tour summary linking into the reference.".to_string(),
            concepts: Vec::new(),
            modules: Vec::new(),
            files: Vec::new(),
            body: None,
            body_degraded_sources: Vec::new(),
            verify_notes: Vec::new(),
            body_observability: GenerationObservability::default(),
        }
    }

    // Every chapter — spine and extra alike — gets a readable, position-ordered
    // `NN-<title>` slug. The ordinal prefix fixes tour order on disk; the title
    // suffix keeps the file name legible (#900).
    #[test]
    fn narrative_extras_get_readable_ordinal_slugs() {
        let pages = vec![
            narrative_page("introduction", "Introduction"),
            narrative_page("cli-entrypoints", "CLI Entrypoints"),
            narrative_page("indexing-pipeline", "Indexing Pipeline"),
        ];
        let ordered = normalize_narrative_pages(pages, &[], &[], &[]);
        let slugs: Vec<&str> = ordered.iter().map(|page| page.slug.as_str()).collect();
        assert_eq!(
            slugs,
            vec![
                "01-introduction",
                "02-architecture",
                "03-indexing-pipeline",
                "04-search-rrf",
                "05-codewiki-generation",
                "06-gwiki-vault",
                "07-graph-vector-storage",
                "08-cli-contracts",
                "09-failure-modes",
                "10-contributor-guide",
                "11-cli-entrypoints"
            ]
        );
    }

    // Re-titling an extra chapter moves it to a new readable slug while the
    // ordinal prefix holds its tour position: position 11 stays `11-*`, but the
    // suffix tracks the title. The orphan GC reclaims the old page and the
    // curated layer (the only linker of extras) regenerates in the same run, so
    // nothing dangles (#900).
    #[test]
    fn re_titled_extra_tracks_its_title_slug() {
        let before = normalize_narrative_pages(
            vec![narrative_page("cli-entrypoints", "CLI Entrypoints")],
            &[],
            &[],
            &[],
        );
        let after = normalize_narrative_pages(
            vec![narrative_page("04-cli-entrypoints", "CLI Runtime")],
            &[],
            &[],
            &[],
        );
        assert_eq!(before[10].slug, "11-cli-entrypoints");
        assert_eq!(after[10].slug, "11-cli-runtime");
    }
}
