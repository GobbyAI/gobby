//! Connections enrichment for ingested session summaries.
//!
//! New handoff-format session summaries emit far fewer `[[Entity]]` wikilinks
//! than the old wiki-digest format, starving upkeep's entity clustering. The
//! daemon handoff prompt is a resume surface and must stay untouched, so the
//! digest funnel enriches instead: every summary body ingested through
//! `ingest_session_wiki_file_without_index` gains a wikilinked
//! `## Connections` section. Inline `[[links]]` already present in the body
//! are gathered deterministically; when AI resolves, one bounded single-shot
//! completion extracts additional entities from the summary text. A body
//! whose Connections section already carries wikilinks (the old wiki-digest
//! format) is left untouched.

/// Cap on rendered Connections entries; inline links win over extracted ones.
const MAX_CONNECTIONS: usize = 12;

/// Longest entity name accepted into a `[[...]]` target.
const MAX_ENTITY_CHARS: usize = 64;

/// Char budget for the summary excerpt handed to the extraction completion.
#[cfg(any(feature = "ai", test))]
const EXTRACTION_BODY_BUDGET: usize = 6_000;

// Thread-local so a test asserting on the count only observes its own
// thread's `resolve()` calls — a process-global counter races every other
// test in the parallel run. The sync path constructs the enricher on its
// caller's thread, so a test always sees its own sync's calls.
#[cfg(test)]
thread_local! {
    static RESOLVE_COUNT: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
}

/// Per-batch Connections enricher.
///
/// Construct once with [`ConnectionsEnricher::resolve`]. The deterministic
/// inline-link layer always works; the extraction completion only runs when
/// text generation resolves to a Daemon or Direct route (and the `ai` feature
/// is compiled in).
pub(crate) struct ConnectionsEnricher {
    #[cfg(feature = "ai")]
    ai: Option<AiRouted>,
}

#[cfg(feature = "ai")]
struct AiRouted {
    context: gobby_core::ai_context::AiContext,
    route: gobby_core::config::AiRouting,
    /// Resolved tier->profile target for the Standard (`feature_low`)
    /// text-generate tier, used on the Direct route only.
    target: Option<gobby_core::ai::generation::DirectGenerationTarget>,
}

impl ConnectionsEnricher {
    #[cfg(test)]
    pub(crate) fn reset_resolve_count_for_test() {
        RESOLVE_COUNT.with(|count| count.set(0));
    }

    #[cfg(test)]
    pub(crate) fn resolve_count_for_test() -> usize {
        RESOLVE_COUNT.with(std::cell::Cell::get)
    }

    #[cfg(test)]
    fn count_resolve_for_test() {
        RESOLVE_COUNT.with(|count| count.set(count.get() + 1));
    }

    /// Resolve AI config once for the whole sync batch. AI being unresolvable
    /// never disables the enricher — it degrades to the inline-link layer.
    #[cfg(feature = "ai")]
    pub(crate) fn resolve() -> Self {
        #[cfg(test)]
        Self::count_resolve_for_test();

        use gobby_core::ai::effective_route;
        use gobby_core::ai::generation::{
            GenerationTier, profile_for_tier, resolve_direct_generation_target,
        };
        use gobby_core::ai_context::{AiContext, AiContextOptions};
        use gobby_core::config::{AiCapability, AiRouting};

        let mut source = match crate::support::config::hub_ai_config_source("gwiki sync-sessions") {
            Ok(source) => source,
            Err(error) => {
                log::warn!("connections enrichment: could not resolve AI config: {error}");
                return Self { ai: None };
            }
        };
        let context = AiContext::resolve_with_options(
            None,
            &mut source,
            AiContextOptions {
                no_ai: false,
                forced_routing: None,
            },
        );
        let route = effective_route(&context, AiCapability::TextGenerate);
        let ai = matches!(route, AiRouting::Daemon).then(|| {
            let target = matches!(route, AiRouting::Daemon).then(|| {
                resolve_direct_generation_target(
                    &mut source,
                    &profile_for_tier(GenerationTier::Standard, None),
                )
            });
            AiRouted {
                context,
                route,
                target,
            }
        });
        Self { ai }
    }

    #[cfg(not(feature = "ai"))]
    pub(crate) fn resolve() -> Self {
        #[cfg(test)]
        Self::count_resolve_for_test();

        Self {}
    }

    /// Inline-link layer only — tests must never resolve a live AI route.
    #[cfg(test)]
    pub(super) fn deterministic() -> Self {
        #[cfg(feature = "ai")]
        {
            Self { ai: None }
        }
        #[cfg(not(feature = "ai"))]
        {
            Self {}
        }
    }

    /// Return the enriched body, or `None` when the body already carries a
    /// wikilinked Connections section or no entities were found. Never fails;
    /// extraction errors degrade to the inline-link layer.
    pub(crate) fn enrich_body(&self, body: &str) -> Option<String> {
        if connections_section(body).is_some_and(|(start, end)| body[start..end].contains("[[")) {
            return None;
        }
        let mut entities = inline_link_targets(body);
        #[cfg(feature = "ai")]
        if let Some(ai) = &self.ai {
            for entity in self.extract_entities(ai, body) {
                if !entities
                    .iter()
                    .any(|existing| existing.eq_ignore_ascii_case(&entity))
                {
                    entities.push(entity);
                }
            }
        }
        entities.truncate(MAX_CONNECTIONS);
        if entities.is_empty() {
            return None;
        }
        Some(apply_connections_section(body, &entities))
    }

    /// One bounded single-shot completion on the Standard text-generate tier.
    #[cfg(feature = "ai")]
    fn extract_entities(&self, ai: &AiRouted, body: &str) -> Vec<String> {
        use gobby_core::ai::generation::{GenerationTier, generate_one_shot};

        let prompt = entity_extraction_prompt(body);
        let result = generate_one_shot(
            &ai.context,
            ai.route,
            GenerationTier::Standard,
            None,
            ai.target.as_ref(),
            &prompt,
            Some("You extract wiki entity names from session summaries."),
            None,
        );
        match result {
            Ok(result) => entities_from_model_output(&result.text),
            Err(error) => {
                log::warn!("connections enrichment: entity extraction failed: {error}");
                Vec::new()
            }
        }
    }
}

#[cfg(any(feature = "ai", test))]
pub(super) fn entity_extraction_prompt(body: &str) -> String {
    let excerpt: String = body.chars().take(EXTRACTION_BODY_BUDGET).collect();
    format!(
        "List up to {MAX_CONNECTIONS} named entities this session summary is about: tools, \
         systems, services, libraries, projects, and durable concepts. One entity per line, \
         the exact name only - no brackets, no bullets, no commentary. Skip generic words, \
         file paths, and one-off identifiers.\n\nSummary:\n{excerpt}"
    )
}

/// Byte range of the `## Connections` section body (heading line excluded,
/// running to the next `## ` heading or end of body), when the heading exists.
fn connections_section(body: &str) -> Option<(usize, usize)> {
    let mut offset = 0usize;
    let mut start: Option<usize> = None;
    for line in body.split_inclusive('\n') {
        let trimmed = line.trim_end();
        if let Some(section_start) = start {
            if trimmed.starts_with("## ") {
                return Some((section_start, offset));
            }
        } else if trimmed.trim() == "## Connections" {
            start = Some(offset + line.len());
        }
        offset += line.len();
    }
    start.map(|section_start| (section_start, body.len()))
}

/// Inline `[[target]]` / `[[target|alias]]` targets in order of first
/// appearance, deduplicated case-insensitively.
fn inline_link_targets(body: &str) -> Vec<String> {
    let mut targets: Vec<String> = Vec::new();
    let mut rest = body;
    while let Some(open) = rest.find("[[") {
        rest = &rest[open + 2..];
        let Some(close) = rest.find("]]") else {
            break;
        };
        let raw = &rest[..close];
        rest = &rest[close + 2..];
        let target = raw.split('|').next().unwrap_or_default();
        if let Some(entity) = sanitize_entity(target)
            && !targets
                .iter()
                .any(|existing| existing.eq_ignore_ascii_case(&entity))
        {
            targets.push(entity);
        }
    }
    targets
}

/// Parse extraction output: one entity per line, tolerating bullets, numbered
/// lists, and stray brackets. Order is preserved; duplicates are dropped.
#[cfg_attr(not(feature = "ai"), allow(dead_code))]
fn entities_from_model_output(output: &str) -> Vec<String> {
    let mut entities: Vec<String> = Vec::new();
    for line in output.lines() {
        let line = line
            .trim()
            .trim_start_matches(['-', '*', '+'])
            .trim_start_matches(|character: char| character.is_ascii_digit())
            .trim_start_matches(['.', ')'])
            .trim();
        let line = line
            .strip_prefix("[[")
            .and_then(|inner| inner.strip_suffix("]]"))
            .unwrap_or(line);
        if let Some(entity) = sanitize_entity(line)
            && !entities
                .iter()
                .any(|existing| existing.eq_ignore_ascii_case(&entity))
        {
            entities.push(entity);
        }
    }
    entities
}

/// Accept an entity name only when it forms a safe `[[...]]` target: bounded
/// length, no wikilink/markdown structure characters, and at least one
/// alphanumeric character.
fn sanitize_entity(raw: &str) -> Option<String> {
    let entity = raw.trim().trim_matches('`').trim();
    if entity.chars().count() < 2 || entity.chars().count() > MAX_ENTITY_CHARS {
        return None;
    }
    if entity.chars().any(|character| {
        matches!(
            character,
            '[' | ']' | '|' | '#' | '^' | '{' | '}' | '<' | '>'
        )
    }) || entity.contains('\n')
    {
        return None;
    }
    if !entity.chars().any(char::is_alphanumeric) {
        return None;
    }
    Some(entity.to_string())
}

/// Insert the entity list under an existing (linkless) `## Connections`
/// heading, or append a new section at the end of the body.
fn apply_connections_section(body: &str, entities: &[String]) -> String {
    let list = entities
        .iter()
        .map(|entity| format!("- [[{entity}]]"))
        .collect::<Vec<_>>()
        .join("\n");
    if let Some((_, section_end)) = connections_section(body) {
        let mut enriched = String::with_capacity(body.len() + list.len() + 2);
        enriched.push_str(body[..section_end].trim_end());
        enriched.push_str("\n\n");
        enriched.push_str(&list);
        enriched.push('\n');
        enriched.push_str(&body[section_end..]);
        return enriched;
    }
    let mut enriched = body.trim_end().to_string();
    enriched.push_str("\n\n## Connections\n\n");
    enriched.push_str(&list);
    enriched.push('\n');
    enriched
}

#[cfg(test)]
mod tests {
    use super::*;

    fn deterministic_enricher() -> ConnectionsEnricher {
        ConnectionsEnricher::deterministic()
    }

    fn section_of(body: &str) -> &str {
        let (start, end) = connections_section(body).expect("connections section");
        &body[start..end]
    }

    #[test]
    fn wikilinked_connections_section_is_left_untouched() {
        let body = "## Summary\n\nAbout [[gcode]].\n\n## Connections\n\n- [[gcode]]\n";
        assert_eq!(deterministic_enricher().enrich_body(body), None);
    }

    #[test]
    fn inline_links_are_gathered_into_a_new_connections_section() {
        let body = "## Summary\n\nWired [[gcode]] into [[FalkorDB]]; verified [[gcode|the index]].";
        let enriched = deterministic_enricher()
            .enrich_body(body)
            .expect("enriched body");
        assert_eq!(section_of(&enriched).trim(), "- [[gcode]]\n- [[FalkorDB]]");
    }

    #[test]
    fn linkless_connections_heading_gains_the_entity_list_in_place() {
        let body = "## Summary\n\nUses [[Qdrant]].\n\n## Connections\n\n(none)\n\n## Next Steps\n\nShip it.";
        let enriched = deterministic_enricher()
            .enrich_body(body)
            .expect("enriched body");
        let section = section_of(&enriched);
        assert!(section.contains("- [[Qdrant]]"), "section: {section}");
        assert!(
            enriched.contains("## Next Steps\n\nShip it."),
            "later sections survive: {enriched}"
        );
    }

    #[test]
    fn body_without_entities_is_left_untouched() {
        assert_eq!(
            deterministic_enricher().enrich_body("## Summary\n\nNothing linkable here."),
            None
        );
    }

    #[test]
    fn model_output_parsing_tolerates_bullets_brackets_and_junk() {
        let output = "- gcode\n* [[FalkorDB]]\n3. LM Studio\n\nGCODE\n`Qdrant`\n[bad|entity]\nx\n";
        assert_eq!(
            entities_from_model_output(output),
            vec!["gcode", "FalkorDB", "LM Studio", "Qdrant"]
        );
    }

    #[test]
    fn entity_cap_prefers_inline_links() {
        let body = (1..=15)
            .map(|index| format!("[[Entity{index}]]"))
            .collect::<Vec<_>>()
            .join(" ");
        let enriched = deterministic_enricher()
            .enrich_body(&body)
            .expect("enriched body");
        let section = section_of(&enriched);
        assert_eq!(section.matches("- [[").count(), MAX_CONNECTIONS);
        assert!(section.contains("- [[Entity1]]"));
        assert!(!section.contains("- [[Entity13]]"));
    }
}
