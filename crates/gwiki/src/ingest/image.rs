use std::path::{Path, PathBuf};

#[cfg(feature = "ai")]
use gobby_core::ai::effective_route;
use gobby_core::ai_context::AiContext;
use gobby_core::config::{AiCapability, AiRouting};

#[cfg(feature = "ai")]
use crate::ai::clients::ProductionVisionClient;
use crate::ingest::{
    IngestResult, index_after_ingest, markdown_metadata, markdown_title, path_to_string,
    write_asset, write_raw_markdown,
};
use crate::sources::{CompileStatus, IngestionMethod, SourceDraftRef, SourceKind, SourceManifest};
use crate::store::WikiIndexStore;
use crate::vision::{
    VisionDegradation, VisionEndpoint, VisionMarkdownResult, VisionRequest,
    write_image_derived_markdown,
};
use crate::{ScopeIdentity, WikiError};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ImageSnapshot {
    pub location: String,
    pub file_name: String,
    pub fetched_at: String,
    pub bytes: Vec<u8>,
    pub mime_type: Option<String>,
    pub width: Option<u32>,
    pub height: Option<u32>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ImageIngestResult {
    pub record: crate::sources::SourceRecord,
    pub raw_path: PathBuf,
    pub asset_path: PathBuf,
    pub derived_path: PathBuf,
    pub vision_degradation: Option<VisionDegradation>,
}

#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
pub fn ingest_image(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    scope: ScopeIdentity,
    snapshot: ImageSnapshot,
) -> Result<ImageIngestResult, WikiError> {
    ingest_image_with_vision(
        vault_root,
        store,
        scope,
        snapshot,
        VisionEndpoint::Unavailable(default_vision_degradation()),
    )
}

#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
pub fn ingest_image_with_production_vision(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    scope: ScopeIdentity,
    ai_context: &AiContext,
    snapshot: ImageSnapshot,
) -> Result<ImageIngestResult, WikiError> {
    let result =
        ingest_image_with_production_vision_without_index(vault_root, scope, ai_context, snapshot)?;
    index_after_ingest(vault_root, store)?;
    Ok(result)
}

pub(crate) fn ingest_image_with_production_vision_without_index(
    vault_root: &Path,
    scope: ScopeIdentity,
    ai_context: &AiContext,
    snapshot: ImageSnapshot,
) -> Result<ImageIngestResult, WikiError> {
    let capability = AiCapability::VisionExtract;

    #[cfg(feature = "ai")]
    {
        let routing = effective_route(ai_context, capability);
        let client = matches!(routing, AiRouting::Daemon | AiRouting::Direct)
            .then(|| ProductionVisionClient::new(ai_context.clone()));
        let endpoint = match client.as_ref() {
            Some(client) => VisionEndpoint::Available(client),
            None => VisionEndpoint::Unavailable(vision_degradation(routing)),
        };
        ingest_image_with_vision_without_index(vault_root, scope, snapshot, endpoint)
    }

    #[cfg(not(feature = "ai"))]
    {
        let endpoint =
            VisionEndpoint::Unavailable(vision_degradation(ai_context.binding(capability).routing));
        ingest_image_with_vision_without_index(vault_root, scope, snapshot, endpoint)
    }
}

#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
pub fn ingest_image_with_vision(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    scope: ScopeIdentity,
    snapshot: ImageSnapshot,
    endpoint: VisionEndpoint<'_>,
) -> Result<ImageIngestResult, WikiError> {
    let result = ingest_image_with_vision_without_index(vault_root, scope, snapshot, endpoint)?;
    index_after_ingest(vault_root, store)?;
    Ok(result)
}

pub(crate) fn ingest_image_with_vision_without_index(
    vault_root: &Path,
    scope: ScopeIdentity,
    snapshot: ImageSnapshot,
    endpoint: VisionEndpoint<'_>,
) -> Result<ImageIngestResult, WikiError> {
    let title = markdown_title(&snapshot.file_name);
    let record = SourceManifest::register_borrowed(
        vault_root,
        SourceDraftRef {
            location: snapshot.location.clone(),
            kind: SourceKind::Image,
            fetched_at: snapshot.fetched_at.clone(),
            content: &snapshot.bytes,
            title: Some(title),
            citation: Some(snapshot.location.clone()),
            license: None,
            ingestion_method: IngestionMethod::Manual,
            compile_status: CompileStatus::Pending,
        },
    )?;
    let asset_path = write_asset(vault_root, &record, &snapshot.file_name, &snapshot.bytes)?;
    let raw_markdown = render_raw_image_markdown(&snapshot, &record.content_hash, &asset_path);
    let raw_path = write_raw_markdown(vault_root, &record, &raw_markdown)?;
    let VisionMarkdownResult {
        path: derived_path,
        degradation,
    } = write_image_derived_markdown(
        vault_root,
        &scope,
        &record,
        VisionRequest {
            file_name: &snapshot.file_name,
            mime_type: snapshot.mime_type.as_deref(),
            asset_path: &asset_path,
            bytes: &snapshot.bytes,
            width: snapshot.width,
            height: snapshot.height,
        },
        endpoint,
    )?;

    Ok(ImageIngestResult {
        record,
        raw_path,
        asset_path,
        derived_path,
        vision_degradation: degradation,
    })
}

impl From<ImageIngestResult> for IngestResult {
    fn from(result: ImageIngestResult) -> Self {
        Self {
            record: result.record,
            raw_path: result.raw_path,
            asset_path: Some(result.asset_path),
        }
    }
}

fn render_raw_image_markdown(
    snapshot: &ImageSnapshot,
    source_hash: &str,
    asset_path: &Path,
) -> String {
    let asset_path = path_to_string(asset_path);
    let mut fields = vec![
        ("source_kind", "image".to_string()),
        ("source_location", snapshot.location.clone()),
        ("fetched_at", snapshot.fetched_at.clone()),
        ("source_hash", source_hash.to_string()),
        ("source_asset", asset_path.clone()),
    ];
    if let Some(mime_type) = &snapshot.mime_type {
        fields.push(("image_mime_type", mime_type.clone()));
    }
    if let Some(width) = snapshot.width {
        fields.push(("image_width", width.to_string()));
    }
    if let Some(height) = snapshot.height {
        fields.push(("image_height", height.to_string()));
    }

    let mut markdown = markdown_metadata(&fields);
    markdown.push_str("# ");
    markdown.push_str(&markdown_title(&snapshot.file_name));
    markdown.push_str("\n\n");
    markdown.push_str("Original image stored under `");
    markdown.push_str(&asset_path);
    markdown.push_str("`.\n");
    markdown
}

#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
fn default_vision_degradation() -> VisionDegradation {
    VisionDegradation {
        reason: gobby_core::degradation::ModalityDegradationReason::MissingEndpoint,
        fallback:
            "Keep raw image assets and surface filename/metadata only; skip visual extraction."
                .to_string(),
    }
}

fn vision_degradation(routing: AiRouting) -> VisionDegradation {
    let reason = match routing {
        AiRouting::Off => gobby_core::degradation::ModalityDegradationReason::Disabled,
        AiRouting::Auto | AiRouting::Daemon | AiRouting::Direct => {
            gobby_core::degradation::ModalityDegradationReason::MissingEndpoint
        }
    };
    VisionDegradation {
        reason,
        fallback: "Keep raw image assets and surface filename/metadata only.".to_string(),
    }
}

#[cfg(test)]
mod tests {
    use gobby_core::indexing::content_hash;

    use super::*;
    use crate::sources::{SourceKind, SourceManifest};
    use crate::store::{MemoryWikiStore, WikiDocumentKind};

    fn sample_snapshot() -> ImageSnapshot {
        ImageSnapshot {
            location: "/tmp/diagram.png".to_string(),
            file_name: "diagram.png".to_string(),
            fetched_at: "2026-05-29T20:30:00Z".to_string(),
            bytes: b"\x89PNG\r\n\x1a\nimage-bytes\n".to_vec(),
            mime_type: Some("image/png".to_string()),
            width: Some(640),
            height: Some(480),
        }
    }

    #[test]
    fn stores_original_image() {
        let temp = tempfile::tempdir().expect("tempdir");
        let snapshot = sample_snapshot();
        let expected_hash = content_hash(&snapshot.bytes);
        let mut store = MemoryWikiStore::default();

        let result = ingest_image(
            temp.path(),
            &mut store,
            ScopeIdentity::topic("field-work"),
            snapshot.clone(),
        )
        .expect("ingest image");

        assert_eq!(
            result.asset_path.parent(),
            Some(PathBuf::from("raw/assets").as_path())
        );
        assert_eq!(
            std::fs::read(temp.path().join(&result.asset_path)).expect("asset bytes"),
            snapshot.bytes
        );
        let raw =
            std::fs::read_to_string(temp.path().join(&result.raw_path)).expect("raw markdown");
        assert!(raw.contains("source_kind: image"));
        assert!(raw.contains("source_asset: raw/assets/"));

        let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
        assert_eq!(manifest.entries.len(), 1);
        assert_eq!(manifest.entries[0].kind, SourceKind::Image);
        assert_eq!(manifest.entries[0].content_hash, expected_hash);
    }

    #[test]
    fn image_metadata_is_scope_indexed() {
        let temp = tempfile::tempdir().expect("tempdir");
        let mut store = MemoryWikiStore::default();

        let result = ingest_image(
            temp.path(),
            &mut store,
            ScopeIdentity::project("project-123"),
            sample_snapshot(),
        )
        .expect("ingest image");

        let document = store
            .documents
            .get(&result.derived_path)
            .expect("derived image document indexed");
        assert_eq!(document.kind, WikiDocumentKind::SourceNote);
        assert!(document.body.contains("scope_kind: project"));
        assert!(document.body.contains("scope_id: project-123"));
        assert!(document.body.contains("image_width: \"640\""));
        assert!(document.body.contains("image_height: \"480\""));
        assert!(store.sources.contains_key(&result.derived_path));
    }

    #[cfg(feature = "ai")]
    #[test]
    fn production_vision_writes_description_and_ocr() {
        let response = r#"{"model":"gpt-4.1-mini","choices":[{"message":{"content":"{\"description\":\"A labeled wiring diagram\",\"ocr_text\":\"VCC GND Sensor\"}"}}]}"#;
        let (api_base, request) = spawn_vision_server(response);
        let context = test_ai_context(&api_base);
        let temp = tempfile::tempdir().expect("tempdir");
        let mut store = MemoryWikiStore::default();

        let result = ingest_image_with_production_vision(
            temp.path(),
            &mut store,
            ScopeIdentity::topic("field-work"),
            &context,
            sample_snapshot(),
        )
        .expect("ingest image with production vision");
        let request = request.join().expect("vision test server thread joins");
        let request = request.expect("vision request was captured");

        assert!(request.starts_with("POST /v1/chat/completions HTTP/1.1"));
        assert!(request.contains("data:image/png;base64,"));
        assert!(result.vision_degradation.is_none());

        let document = store
            .documents
            .get(&result.derived_path)
            .expect("derived image document indexed");
        assert!(document.body.contains("vision_status: extracted"));
        assert!(document.body.contains("vision_model: gpt-4.1-mini"));
        assert!(
            document
                .body
                .contains("## Vision Description\n\nA labeled wiring diagram")
        );
        assert!(document.body.contains("## OCR Text\n\nVCC GND Sensor"));
    }

    #[cfg(feature = "ai")]
    fn test_ai_context(api_base: &str) -> gobby_core::ai_context::AiContext {
        use gobby_core::ai_context::{AiBindings, AiLimiter};
        use gobby_core::config::{AiRouting, AiTuning, CapabilityBinding};

        let binding = CapabilityBinding {
            routing: AiRouting::Direct,
            transport: None,
            api_base: Some(api_base.to_string()),
            api_key: None,
            model: Some("gpt-4.1-mini".to_string()),
            provider: None,
            task: None,
            language: None,
            target_lang: None,
            profile: None,
        };

        gobby_core::ai_context::AiContext {
            bindings: AiBindings {
                embed: binding.clone(),
                audio_transcribe: binding.clone(),
                audio_translate: binding.clone(),
                vision_extract: binding.clone(),
                text_generate: binding,
            },
            tuning: AiTuning {
                max_concurrency: 1,
                keep_alive: None,
            },
            limiter: AiLimiter::new(1),
            project_id: None,
        }
    }

    #[cfg(feature = "ai")]
    fn spawn_vision_server(response: &'static str) -> (String, crate::test_http::RequestHandle) {
        crate::test_http::spawn_json_response(response).expect("spawn test server")
    }
}
