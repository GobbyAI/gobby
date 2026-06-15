use std::path::Path;

use crate::WikiError;
use crate::ingest::lowercase_extension;
use crate::sources::SourceKind;

use super::TEXT_INLINE_LIMIT_BYTES;

pub(super) fn detect_source_kind(path: &Path) -> SourceKind {
    match lowercase_extension(path).as_deref() {
        Some("pdf") => SourceKind::Pdf,
        Some("docx" | "xlsx" | "xls" | "ods" | "pptx") => SourceKind::Office,
        Some("html" | "htm") => SourceKind::Html,
        Some("mp3" | "wav" | "m4a" | "aac" | "flac" | "ogg" | "opus") => SourceKind::Audio,
        Some("png" | "jpg" | "jpeg" | "gif" | "webp" | "bmp" | "tiff") => SourceKind::Image,
        Some("mp4" | "mov" | "m4v" | "webm" | "mkv") => SourceKind::Video,
        Some("md" | "markdown") => SourceKind::Markdown,
        Some(
            "txt" | "text" | "csv" | "json" | "jsonl" | "xml" | "yaml" | "yml" | "toml" | "log"
            | "ini" | "env" | "properties" | "conf" | "sql" | "sh" | "bash",
        ) => SourceKind::Text,
        _ => SourceKind::File,
    }
}

pub(super) fn source_location(vault_root: &Path, path: &Path) -> String {
    let display_path = if let Ok(relative) = path.strip_prefix(vault_root) {
        relative.to_path_buf()
    } else if let (Ok(canonical_root), Ok(canonical_path)) =
        (vault_root.canonicalize(), path.canonicalize())
    {
        canonical_path
            .strip_prefix(&canonical_root)
            .map(Path::to_path_buf)
            .unwrap_or(canonical_path)
    } else {
        path.to_path_buf()
    };
    display_path.to_string_lossy().replace('\\', "/")
}

pub(super) fn should_store_asset(kind: &SourceKind, byte_len: usize) -> bool {
    matches!(kind, SourceKind::Text if byte_len > TEXT_INLINE_LIMIT_BYTES)
        || matches!(
            kind,
            SourceKind::Audio
                | SourceKind::Image
                | SourceKind::Video
                | SourceKind::Pdf
                | SourceKind::Office
                | SourceKind::Html
                | SourceKind::File
        )
}

pub(super) fn read_source_file(path: &Path) -> Result<Vec<u8>, WikiError> {
    std::fs::read(path).map_err(|error| WikiError::Io {
        action: "read source file",
        path: Some(path.to_path_buf()),
        source: error,
    })
}
