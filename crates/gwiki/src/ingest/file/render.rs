use std::path::Path;

use crate::ingest::{markdown_metadata, path_to_string, text_from_utf8_lossy};
use crate::sources::SourceKind;

pub(super) fn render_file_markdown(
    title: &str,
    location: &str,
    fetched_at: &str,
    source_hash: &str,
    kind: &SourceKind,
    bytes: &[u8],
    asset_path: Option<&Path>,
) -> String {
    let mut fields = vec![
        ("source_kind", kind.to_string()),
        ("source_location", location.to_string()),
        ("fetched_at", fetched_at.to_string()),
        ("source_hash", source_hash.to_string()),
    ];
    if let Some(asset_path) = asset_path {
        fields.push(("source_asset", path_to_string(asset_path)));
    }

    let mut markdown = markdown_metadata(&fields);
    markdown.push_str("# ");
    markdown.push_str(title);
    markdown.push_str("\n\n");

    match (kind, asset_path) {
        (SourceKind::Markdown | SourceKind::Text | SourceKind::Stdin, None) => {
            markdown.push_str(&text_from_utf8_lossy(bytes));
            if !markdown.ends_with('\n') {
                markdown.push('\n');
            }
        }
        _ => {
            if let Some(asset_path) = asset_path {
                markdown.push_str("Original artifact stored under `");
                markdown.push_str(&path_to_string(asset_path));
                markdown.push_str("`.\n");
            } else {
                markdown.push_str(
                    "Original artifact was recorded in the source manifest; no raw asset was written.\n",
                );
            }
        }
    }

    markdown
}
