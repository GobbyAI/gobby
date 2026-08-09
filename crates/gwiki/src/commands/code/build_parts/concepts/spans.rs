use super::super::super::*;
use super::types::*;

pub(super) fn all_input_spans(files: &[FileDoc], modules: &[ModuleDoc]) -> Vec<SourceSpan> {
    let mut spans = std::collections::BTreeSet::new();
    for file in files {
        spans.extend(file.source_spans.iter().cloned());
    }
    for module in modules {
        spans.extend(module.source_spans.iter().cloned());
    }
    spans.into_iter().collect()
}

pub(super) fn narrative_spans(
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

pub(super) fn item_spans(
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
