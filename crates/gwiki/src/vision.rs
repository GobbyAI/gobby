use std::path::{Path, PathBuf};

use crate::ingest::{markdown_metadata, markdown_title, path_to_string, single_line};
use crate::sources::SourceRecord;
use crate::{ScopeIdentity, WikiError};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VisionExtraction {
    pub description: String,
    pub ocr_text: Option<String>,
    pub metadata: Vec<(String, String)>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VisionDegradation {
    pub reason: String,
    pub fallback: String,
}

pub trait VisionClient {
    fn extract(&self, request: &VisionRequest<'_>) -> Result<VisionExtraction, WikiError>;
}

#[derive(Debug, Clone, Copy)]
pub struct VisionRequest<'a> {
    pub file_name: &'a str,
    pub mime_type: Option<&'a str>,
    pub asset_path: &'a Path,
    pub bytes: &'a [u8],
    pub width: Option<u32>,
    pub height: Option<u32>,
}

pub enum VisionEndpoint<'a> {
    Available(&'a dyn VisionClient),
    Unavailable(VisionDegradation),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VisionMarkdownResult {
    pub path: PathBuf,
    pub degradation: Option<VisionDegradation>,
}

pub fn write_image_derived_markdown(
    vault_root: &Path,
    scope: &ScopeIdentity,
    record: &SourceRecord,
    request: VisionRequest<'_>,
    endpoint: VisionEndpoint<'_>,
) -> Result<VisionMarkdownResult, WikiError> {
    let (extraction, degradation) = match endpoint {
        VisionEndpoint::Available(client) => match client.extract(&request) {
            Ok(extraction) => (Some(extraction), None),
            Err(error) => (
                None,
                Some(VisionDegradation {
                    reason: "vision_error".to_string(),
                    fallback: format!(
                        "Vision extraction failed: {error}; keep raw image assets and surface filename/metadata only."
                    ),
                }),
            ),
        },
        VisionEndpoint::Unavailable(degradation) => (None, Some(degradation)),
    };
    let relative_path = derived_markdown_path(record);
    let path = vault_root.join(&relative_path);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|error| WikiError::Io {
            action: "create vision derived markdown directory",
            path: Some(parent.to_path_buf()),
            source: error,
        })?;
    }

    let markdown =
        render_image_derived_markdown(scope, record, request, extraction, degradation.as_ref());
    std::fs::write(&path, markdown).map_err(|error| WikiError::Io {
        action: "write vision derived markdown",
        path: Some(path),
        source: error,
    })?;

    Ok(VisionMarkdownResult {
        path: relative_path,
        degradation,
    })
}

fn derived_markdown_path(record: &SourceRecord) -> PathBuf {
    PathBuf::from("wiki")
        .join("sources")
        .join(format!("{}.md", record.id))
}

fn render_image_derived_markdown(
    scope: &ScopeIdentity,
    record: &SourceRecord,
    request: VisionRequest<'_>,
    extraction: Option<VisionExtraction>,
    degradation: Option<&VisionDegradation>,
) -> String {
    let asset_path = path_to_string(request.asset_path);
    let raw_path = format!("raw/{}.md", record.id);
    let mut fields = vec![
        ("title".to_string(), markdown_title(request.file_name)),
        ("source_kind".to_string(), "image".to_string()),
        ("source_location".to_string(), record.location.clone()),
        ("source_hash".to_string(), record.content_hash.clone()),
        ("source_asset".to_string(), asset_path.clone()),
        ("source_raw".to_string(), raw_path.clone()),
        ("fetched_at".to_string(), record.fetched_at.clone()),
        ("scope_kind".to_string(), scope.kind.clone()),
        ("scope_id".to_string(), scope.id.clone()),
        (
            "vision_status".to_string(),
            if extraction.is_some() {
                "extracted".to_string()
            } else {
                "unavailable".to_string()
            },
        ),
        ("image_bytes".to_string(), request.bytes.len().to_string()),
    ];
    if let Some(mime_type) = request.mime_type {
        fields.push(("image_mime_type".to_string(), mime_type.to_string()));
    }
    if let Some(width) = request.width {
        fields.push(("image_width".to_string(), width.to_string()));
    }
    if let Some(height) = request.height {
        fields.push(("image_height".to_string(), height.to_string()));
    }
    if let Some(degradation) = degradation {
        fields.push(("vision_degradation".to_string(), degradation.reason.clone()));
    }
    if let Some(extraction) = &extraction {
        for (key, value) in &extraction.metadata {
            fields.push((format!("vision_{key}"), value.clone()));
        }
    }

    let mut markdown = {
        let field_refs = fields
            .iter()
            .map(|(key, value)| (key.as_str(), value.clone()))
            .collect::<Vec<_>>();
        markdown_metadata(&field_refs)
    };
    markdown.push_str("# ");
    markdown.push_str(&markdown_title(request.file_name));
    markdown.push_str("\n\n");
    markdown.push_str("Original image: `");
    markdown.push_str(&asset_path);
    markdown.push_str("`\n\n");
    markdown.push_str("Raw source: `");
    markdown.push_str(&raw_path);
    markdown.push_str("`\n\n");

    if let Some(extraction) = extraction {
        markdown.push_str("## Vision Description\n\n");
        markdown.push_str(&single_line(&extraction.description));
        markdown.push_str("\n\n");
        if let Some(ocr_text) = extraction.ocr_text.filter(|text| !text.trim().is_empty()) {
            markdown.push_str("## OCR Text\n\n");
            markdown.push_str(&single_line(&ocr_text));
            markdown.push_str("\n\n");
        }
        if !extraction.metadata.is_empty() {
            markdown.push_str("## Vision Metadata\n\n");
            for (key, value) in extraction.metadata {
                markdown.push_str("- ");
                markdown.push_str(&single_line(&key));
                markdown.push_str(": ");
                markdown.push_str(&single_line(&value));
                markdown.push('\n');
            }
            markdown.push('\n');
        }
    } else if let Some(degradation) = degradation {
        markdown.push_str("## Vision Unavailable\n\n");
        markdown.push_str(&single_line(&degradation.reason));
        markdown.push_str(": ");
        markdown.push_str(&single_line(&degradation.fallback));
        markdown.push_str("\n\n");
    }

    markdown.push_str("## Source References\n\n");
    markdown.push_str("- Raw source: `");
    markdown.push_str(&raw_path);
    markdown.push_str("`\n");
    markdown.push_str("- Original image: `");
    markdown.push_str(&asset_path);
    markdown.push_str("`\n");
    if let Some(citation) = &record.citation {
        markdown.push_str("- Citation: ");
        markdown.push_str(&single_line(citation));
        markdown.push('\n');
    }
    markdown
}

#[cfg(test)]
mod tests {
    use std::cell::Cell;

    use super::*;
    use crate::sources::{CompileStatus, IngestionMethod, SourceDraft, SourceKind, SourceManifest};

    struct FakeVisionClient {
        calls: Cell<usize>,
    }

    struct FailingVisionClient;

    impl VisionClient for FakeVisionClient {
        fn extract(&self, _request: &VisionRequest<'_>) -> Result<VisionExtraction, WikiError> {
            self.calls.set(self.calls.get() + 1);
            Ok(VisionExtraction {
                description: "A labeled circuit diagram with two power rails.".to_string(),
                ocr_text: Some("VCC GND Sensor".to_string()),
                metadata: vec![("model".to_string(), "fake-vision".to_string())],
            })
        }
    }

    impl VisionClient for FailingVisionClient {
        fn extract(&self, _request: &VisionRequest<'_>) -> Result<VisionExtraction, WikiError> {
            Err(WikiError::Daemon {
                endpoint: "/api/chat/attachments",
                message: "temporarily unavailable".to_string(),
            })
        }
    }

    fn record_for(temp: &Path) -> SourceRecord {
        SourceManifest::register(
            temp,
            SourceDraft {
                location: "/tmp/circuit.png".to_string(),
                kind: SourceKind::Image,
                fetched_at: "2026-05-29T20:45:00Z".to_string(),
                content: b"image-bytes".to_vec(),
                title: Some("circuit.png".to_string()),
                citation: Some("/tmp/circuit.png".to_string()),
                license: None,
                ingestion_method: IngestionMethod::Manual,
                compile_status: CompileStatus::Pending,
            },
        )
        .expect("register source")
    }

    #[test]
    fn vision_writes_derived_markdown() {
        let temp = tempfile::tempdir().expect("tempdir");
        let record = record_for(temp.path());
        let asset_path = PathBuf::from("raw/assets/image.png");
        let client = FakeVisionClient {
            calls: Cell::new(0),
        };

        let result = write_image_derived_markdown(
            temp.path(),
            &ScopeIdentity::topic("electronics"),
            &record,
            VisionRequest {
                file_name: "circuit.png",
                mime_type: Some("image/png"),
                asset_path: &asset_path,
                bytes: b"image-bytes",
                width: Some(1024),
                height: Some(768),
            },
            VisionEndpoint::Available(&client),
        )
        .expect("write derived markdown");

        assert_eq!(client.calls.get(), 1);
        assert_eq!(
            result.path,
            PathBuf::from("wiki/sources").join(format!("{}.md", record.id))
        );
        assert!(result.degradation.is_none());

        let markdown =
            std::fs::read_to_string(temp.path().join(&result.path)).expect("derived markdown");
        assert!(markdown.contains("source_kind: image"));
        assert!(markdown.contains("scope_kind: topic"));
        assert!(markdown.contains("scope_id: electronics"));
        assert!(markdown.contains("source_asset: raw/assets/image.png"));
        assert!(markdown.contains("## Vision Description"));
        assert!(markdown.contains("A labeled circuit diagram with two power rails."));
        assert!(markdown.contains("## OCR Text"));
        assert!(markdown.contains("VCC GND Sensor"));
        assert!(markdown.contains("## Source References"));
        assert!(markdown.contains("raw/assets/image.png"));
    }

    #[test]
    fn missing_vision_degrades() {
        let temp = tempfile::tempdir().expect("tempdir");
        let record = record_for(temp.path());
        let asset_path = PathBuf::from("raw/assets/image.png");
        std::fs::create_dir_all(temp.path().join("raw/assets")).expect("asset dir");
        std::fs::write(temp.path().join(&asset_path), b"image-bytes").expect("asset");

        let result = write_image_derived_markdown(
            temp.path(),
            &ScopeIdentity::project("project-123"),
            &record,
            VisionRequest {
                file_name: "circuit.png",
                mime_type: Some("image/png"),
                asset_path: &asset_path,
                bytes: b"image-bytes",
                width: None,
                height: None,
            },
            VisionEndpoint::Unavailable(VisionDegradation {
                reason: "missing_endpoint".to_string(),
                fallback: "Keep raw image assets and surface filename/metadata only.".to_string(),
            }),
        )
        .expect("write degraded markdown");

        let degradation = result.degradation.expect("degradation");
        assert_eq!(degradation.reason, "missing_endpoint");
        assert_eq!(
            std::fs::read(temp.path().join(&asset_path)).expect("asset remains"),
            b"image-bytes"
        );

        let markdown =
            std::fs::read_to_string(temp.path().join(&result.path)).expect("derived markdown");
        assert!(markdown.contains("vision_status: unavailable"));
        assert!(markdown.contains("vision_degradation: missing_endpoint"));
        assert!(markdown.contains("Keep raw image assets"));
        assert!(markdown.contains("Original image: `raw/assets/image.png`"));
    }

    #[test]
    fn vision_client_error_degrades() {
        let temp = tempfile::tempdir().expect("tempdir");
        let record = record_for(temp.path());
        let asset_path = PathBuf::from("raw/assets/image.png");

        let result = write_image_derived_markdown(
            temp.path(),
            &ScopeIdentity::project("project-123"),
            &record,
            VisionRequest {
                file_name: "circuit.png",
                mime_type: Some("image/png"),
                asset_path: &asset_path,
                bytes: b"image-bytes",
                width: None,
                height: None,
            },
            VisionEndpoint::Available(&FailingVisionClient),
        )
        .expect("vision error degrades");

        let degradation = result.degradation.expect("degradation");
        assert_eq!(degradation.reason, "vision_error");
        assert!(degradation.fallback.contains("/api/chat/attachments"));

        let markdown =
            std::fs::read_to_string(temp.path().join(&result.path)).expect("derived markdown");
        assert!(markdown.contains("vision_status: unavailable"));
        assert!(markdown.contains("vision_degradation: vision_error"));
        assert!(markdown.contains("## Source References"));
        assert!(markdown.contains("raw/assets/image.png"));
    }
}
