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

// Domain-agnostic handbook spine. The slugs/titles describe any indexed
// codebase (not gobby's own subsystems) so the curated tour generalizes; each
// chapter's `patterns` are substrings matched (lowercased) against concept,
// module, and file identifiers to pull in the most relevant grounded reference
// links. `slug` must equal `slugify(title)` so the spine-matching in
// `normalize_narrative_pages` reattaches a model-supplied page to its chapter.
const DEFAULT_CHAPTERS: [DefaultChapter; 9] = [
    DefaultChapter {
        slug: "overview",
        title: "Overview",
        summary: "Start with the system purpose, the major crates or components, runtime modes, and the shortest path into the grounded reference.",
        patterns: &[],
    },
    DefaultChapter {
        slug: "architecture",
        title: "Architecture",
        summary: "Explain the top-level structure: the layers, ownership boundaries, shared foundations, and why the components are separated the way they are.",
        patterns: &[
            "architecture",
            "core",
            "config",
            "context",
            "module",
            "layer",
            "boundary",
            "foundation",
            "shared",
        ],
    },
    DefaultChapter {
        slug: "capabilities",
        title: "Capabilities",
        summary: "Survey the primary capabilities the system provides and the modules that implement each feature.",
        patterns: &[
            "feature",
            "capability",
            "service",
            "engine",
            "command",
            "handler",
            "query",
            "search",
            "index",
            "process",
            "ingest",
        ],
    },
    DefaultChapter {
        slug: "workflows",
        title: "Workflows",
        summary: "Trace the end-to-end workflows: how a request or input flows through the pipeline stages from entry to result.",
        patterns: &[
            "workflow",
            "pipeline",
            "orchestrat",
            "flow",
            "stage",
            "step",
            "dispatch",
            "sequence",
            "task",
            "job",
        ],
    },
    DefaultChapter {
        slug: "getting-started",
        title: "Getting Started",
        summary: "Show how to set the system up and run it for the first time: installation, bootstrap, initialization, and required configuration.",
        patterns: &[
            "setup",
            "init",
            "bootstrap",
            "install",
            "provision",
            "quickstart",
            "onboard",
            "configure",
        ],
    },
    DefaultChapter {
        slug: "operations",
        title: "Operations",
        summary: "Cover running the system in practice: the daemon or services, runtime lifecycle, synchronization, health, and monitoring.",
        patterns: &[
            "daemon",
            "server",
            "runtime",
            "deploy",
            "monitor",
            "health",
            "sync",
            "lifecycle",
            "transport",
            "schedule",
        ],
    },
    DefaultChapter {
        slug: "data-model",
        title: "Data Model",
        summary: "Describe the core data: the schemas, types, stored entities, and how the datastores and projections relate.",
        patterns: &[
            "model", "schema", "type", "struct", "store", "database", "table", "entity", "record",
            "vector", "graph", "persist",
        ],
    },
    DefaultChapter {
        slug: "cli-api",
        title: "CLI-API",
        summary: "Document the surfaces callers use: CLI commands, flags, contracts, output modes, and programmatic APIs.",
        patterns: &[
            "cli", "command", "contract", "api", "dispatch", "output", "flag", "arg", "route",
            "endpoint", "main",
        ],
    },
    DefaultChapter {
        slug: "troubleshooting",
        title: "Troubleshooting",
        summary: "Surface failure handling: degraded states, fallbacks, unavailable dependencies, recovery paths, and verification notes.",
        patterns: &[
            "degrad",
            "fallback",
            "unavailable",
            "failure",
            "error",
            "verify",
            "recover",
            "diagnos",
            "cleanup",
            "prune",
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
         Overview, Architecture, Capabilities, Workflows, Getting Started, Operations, Data Model, CLI-API, Troubleshooting.\n\
         Render enumerable facts (CLI commands, configuration keys, data models, public API symbols) as compact Markdown tables, grounded in the supplied identifiers.\n\n\
         Schema:\n\
         {\"concept_modules\":[{\"title\":\"...\",\"summary\":\"...\",\"modules\":[\"...\"],\"files\":[\"...\"]}],\
         \"sections\":[{\"title\":\"...\",\"summary\":\"...\",\"concepts\":[\"concept title\"]}],\
         \"narrative_pages\":[{\"slug\":\"overview\",\"title\":\"...\",\"summary\":\"...\",\"concepts\":[\"concept title\"],\"modules\":[\"...\"],\"files\":[\"...\"]}]}\n\n\
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
    if let Some(plan) = parse_plan_json(json) {
        return Some(plan);
    }
    // A tool-using model may wrap the plan in reasoning or commentary despite
    // the "return only JSON" instruction. Try every balanced object span so an
    // earlier invalid braced aside does not poison recovery.
    parse_plan_from_brace_spans(json)
}

fn parse_plan_from_brace_spans(json: &str) -> Option<CuratedNavigationPlan> {
    let mut starts = Vec::new();
    let mut in_string = false;
    let mut escaped = false;

    for (index, ch) in json.char_indices() {
        if in_string {
            if escaped {
                escaped = false;
            } else if ch == '\\' {
                escaped = true;
            } else if ch == '"' {
                in_string = false;
            }
            continue;
        }

        match ch {
            '"' => in_string = true,
            '{' => starts.push(index),
            '}' => {
                let Some(start) = starts.pop() else {
                    continue;
                };
                let end = index + ch.len_utf8();
                if let Some(plan) = parse_plan_json(&json[start..end]) {
                    return Some(plan);
                }
            }
            _ => {}
        }
    }

    None
}

/// Parse a JSON span into a plan, repairing invalid escape sequences first. A
/// weak model routinely emits Markdown table content inside the JSON string
/// values (e.g. an escaped pipe `\|` from a `json\|text` cell); `\|` is not a
/// valid JSON escape, so the strict parser rejects an otherwise-complete plan
/// and the run falls back to a degenerate structural taxonomy (#993). Repair the
/// stray escapes and retry before giving up.
fn parse_plan_json(json: &str) -> Option<CuratedNavigationPlan> {
    if let Ok(plan) = serde_json::from_str::<CuratedNavigationPlan>(json) {
        return Some(plan);
    }
    let repaired = repair_json_escapes(json);
    if repaired != json
        && let Ok(plan) = serde_json::from_str::<CuratedNavigationPlan>(&repaired)
    {
        return Some(plan);
    }
    None
}

/// Drop stray backslashes that do not introduce a valid JSON escape (`"`, `\`,
/// `/`, `b`, `f`, `n`, `r`, `t`, `u`). Valid escapes — including `\\` and
/// `\uXXXX` — pass through unchanged; only the non-standard ones a model emits
/// inside Markdown table content (e.g. `\|`) are unescaped.
fn repair_json_escapes(json: &str) -> String {
    let mut out = String::with_capacity(json.len());
    let mut chars = json.chars();
    while let Some(c) = chars.next() {
        if c != '\\' {
            out.push(c);
            continue;
        }
        match chars.next() {
            Some(next) if matches!(next, '"' | '\\' | '/' | 'b' | 'f' | 'n' | 'r' | 't' | 'u') => {
                out.push('\\');
                out.push(next);
            }
            // Invalid escape (e.g. `\|` from a Markdown table cell): drop the
            // backslash, keep the character.
            Some(next) => out.push(next),
            None => out.push('\\'),
        }
    }
    out
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
    // (`01-overview`, `05-getting-started`, ...). The
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

    #[test]
    fn parse_plan_recovers_json_wrapped_in_reasoning_or_commentary() {
        let clean = r#"{"concept_modules":[],"sections":[],"narrative_pages":[]}"#;
        assert!(parse_plan(clean).is_some(), "clean JSON must parse");
        // A tool-using model may surround the plan with reasoning/commentary
        // despite the "return only JSON" instruction; recover the {...} span.
        let wrapped = format!("Let me investigate first.\n\n{clean}\n\nThat is the plan.");
        assert!(
            parse_plan(&wrapped).is_some(),
            "wrapped JSON must be recovered"
        );
        let wrapped_after_invalid_braces =
            format!("I checked {{not valid json}} before emitting:\n{clean}");
        assert!(
            parse_plan(&wrapped_after_invalid_braces).is_some(),
            "valid JSON after invalid braced commentary must be recovered"
        );
        // Fenced JSON keeps parsing (existing behavior).
        let fenced = format!("```json\n{clean}\n```");
        assert!(parse_plan(&fenced).is_some(), "fenced JSON must parse");
        // Genuinely non-JSON output still fails (no false positive).
        assert!(parse_plan("no json here at all").is_none());
    }

    #[test]
    fn parse_plan_repairs_invalid_markdown_escapes_in_content() {
        // A weak model emits Markdown table content inside a JSON string value;
        // an escaped pipe (`json\|text`) is not a valid JSON escape and would
        // otherwise sink the whole plan into the degenerate fallback (#993).
        let raw = "{\"concept_modules\":[{\"title\":\"Command Interface\",\
            \"summary\":\"The CLI surface.\",\"modules\":[\"crates/gcode/src/cli\"],\"files\":[]}],\
            \"sections\":[{\"slug\":\"cli\",\"title\":\"CLI\",\"summary\":\"Commands.\",\
            \"concepts\":[\"Command Interface\"],\"modules\":[],\"files\":[],\
            \"content\":\"| Flag | Values |\\n| --- | --- |\\n| --format | `json\\|text` |\"}],\
            \"narrative_pages\":[]}";
        // The raw text contains a literal backslash-pipe that serde rejects.
        assert!(
            serde_json::from_str::<CuratedNavigationPlan>(raw).is_err(),
            "precondition: the invalid escape makes strict parsing fail"
        );
        let plan = parse_plan(raw).expect("escape repair must recover the plan");
        assert_eq!(plan.concept_modules.len(), 1);
        assert_eq!(plan.concept_modules[0].title, "Command Interface");
        assert_eq!(plan.sections.len(), 1);
    }

    #[test]
    fn repair_json_escapes_preserves_valid_escapes() {
        // Valid escapes (\\n, \\", \\\\, \\uXXXX) survive; only the stray ones go.
        let input =
            r#"{"a":"line\nbreak","b":"quote\"in","c":"back\\slash","d":"ué","e":"pipe\|x"}"#;
        let repaired = repair_json_escapes(input);
        let value: serde_json::Value =
            serde_json::from_str(&repaired).expect("repaired JSON parses");
        assert_eq!(value["a"], "line\nbreak");
        assert_eq!(value["b"], "quote\"in");
        assert_eq!(value["c"], "back\\slash");
        assert_eq!(value["d"], "u\u{00e9}");
        assert_eq!(value["e"], "pipe|x");
    }

    // Every chapter — spine and extra alike — gets a readable, position-ordered
    // `NN-<title>` slug. The ordinal prefix fixes tour order on disk; the title
    // suffix keeps the file name legible (#900).
    #[test]
    fn narrative_extras_get_readable_ordinal_slugs() {
        let pages = vec![
            narrative_page("overview", "Overview"),
            narrative_page("cli-entrypoints", "CLI Entrypoints"),
            narrative_page("capabilities", "Capabilities"),
        ];
        let ordered = normalize_narrative_pages(pages, &[], &[], &[]);
        let slugs: Vec<&str> = ordered.iter().map(|page| page.slug.as_str()).collect();
        assert_eq!(
            slugs,
            vec![
                "01-overview",
                "02-architecture",
                "03-capabilities",
                "04-workflows",
                "05-getting-started",
                "06-operations",
                "07-data-model",
                "08-cli-api",
                "09-troubleshooting",
                "10-cli-entrypoints"
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
        assert_eq!(before[9].slug, "10-cli-entrypoints");
        assert_eq!(after[9].slug, "10-cli-runtime");
    }
}
