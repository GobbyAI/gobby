/// Union of a curated page's plan-level and per-body degraded-source reason
/// codes, deduplicated. `model-refusal`/`model-prompt-echo`/`model-unavailable`
/// (AI failures) and `grounding-empty` (grounding/structure gaps) stay distinct
/// instead of collapsing to a single blanket code.
pub(super) fn combine_degraded_sources(plan: &[String], body: &[String]) -> Vec<String> {
    plan.iter()
        .chain(body)
        .cloned()
        .collect::<std::collections::BTreeSet<_>>()
        .into_iter()
        .collect()
}

pub(super) fn concept_title(module: &str) -> String {
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

pub(super) fn concept_doc_path(slug: &str) -> String {
    format!("{}.md", concept_doc_stem(slug))
}

pub(super) fn concept_doc_stem(slug: &str) -> String {
    format!("code/concepts/{slug}")
}

pub(super) fn narrative_doc_path(slug: &str) -> String {
    format!("code/narrative/{slug}.md")
}

pub(super) fn slugify(value: &str) -> String {
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
