use std::path::PathBuf;

use crate::sources::SourceRecord;

pub(crate) fn derived_markdown_path(record: &SourceRecord) -> PathBuf {
    PathBuf::from("knowledge")
        .join("sources")
        .join(format!("{}.md", record.id))
}
