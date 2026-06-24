#[allow(clippy::too_many_arguments)]
/// One source-grounded row for a curated page content prompt: a member
/// module/file or a key symbol paired with a `file:line` citation.
/// [`ChildSummary`] alone carries no location, so the content pass would have
/// nothing real to cite; these rows give the model concrete anchors to ground
/// prose against (review #4).
#[derive(Debug, Clone)]
pub struct PageEvidenceRow {
    pub name: String,
    pub kind: String,
    pub citation: String,
    pub summary: String,
}

#[derive(Debug, Clone)]
pub struct SymbolSummary {
    pub name: String,
    pub kind: String,
    pub component_id: String,
    pub component_label: String,
    pub line_start: usize,
    pub line_end: usize,
    pub purpose: String,
}

#[derive(Debug, Clone)]
pub struct ChildSummary {
    pub name: String,
    pub summary: String,
}

/// A retrieved span of real source content fed into an aggregate prompt
/// alongside child summaries.
#[derive(Debug, Clone)]
pub struct SourceExcerpt {
    pub path: String,
    pub line_start: usize,
    pub line_end: usize,
    pub excerpt: String,
}
