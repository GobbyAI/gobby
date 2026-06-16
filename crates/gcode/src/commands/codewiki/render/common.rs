pub(crate) fn model_degraded_sources(degraded: bool) -> Vec<String> {
    if degraded {
        vec!["model-unavailable".to_string()]
    } else {
        Vec::new()
    }
}
