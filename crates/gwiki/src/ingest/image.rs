use std::path::{Path, PathBuf};

#[cfg(feature = "ai")]
use gobby_core::ai::effective_route;
use gobby_core::ai_context::AiContext;
use gobby_core::config::{AiCapability, AiRouting};

#[cfg(feature = "ai")]
use crate::ai::clients::ProductionVisionClient;
use crate::ingest::{
    IngestResult, existing_raw_markdown, index_after_ingest, markdown_metadata, markdown_title,
    path_to_string, write_asset, write_raw_markdown,
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
    index_after_ingest(
        vault_root,
        store,
        &mut crate::progress::ProgressOptions::default(),
    )?;
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
        let client = matches!(routing, AiRouting::Daemon)
            .then(|| ProductionVisionClient::new(ai_context.clone()));
        let endpoint = match client.as_ref() {
            Some(client) => VisionEndpoint::Available(client),
            None => VisionEndpoint::Unavailable(VisionDegradation::for_routing(
                routing,
                "Keep raw image assets and surface filename/metadata only.",
            )),
        };
        ingest_image_with_vision_without_index(vault_root, scope, snapshot, endpoint)
    }

    #[cfg(not(feature = "ai"))]
    {
        let endpoint = VisionEndpoint::Unavailable(VisionDegradation::for_routing(
            ai_context.binding(capability).routing,
            "Keep raw image assets and surface filename/metadata only.",
        ));
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
    index_after_ingest(
        vault_root,
        store,
        &mut crate::progress::ProgressOptions::default(),
    )?;
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
            last_verified_at: snapshot.fetched_at.clone(),
            fetch_provenance: crate::sources::FetchProvenance::Stub,
            content: &snapshot.bytes,
            title: Some(title),
            citation: Some(snapshot.location.clone()),
            license: None,
            ingestion_method: IngestionMethod::Manual,
            compile_status: CompileStatus::Pending,
        },
    )?;
    let asset_path = write_asset(vault_root, &record, &snapshot.file_name, &snapshot.bytes)?;
    // Reuse the first capture on unchanged re-ingest; fresh writes render with
    // the record's stored capture time so recovery re-writes stay
    // byte-identical to the manifest record (#17650).
    let raw_path = match existing_raw_markdown(vault_root, &record) {
        Some(existing) => existing,
        None => {
            let raw_markdown = render_raw_image_markdown(
                &snapshot,
                &record.fetched_at,
                &record.content_hash,
                &asset_path,
            );
            write_raw_markdown(vault_root, &record, &raw_markdown)?
        }
    };
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
    fetched_at: &str,
    source_hash: &str,
    asset_path: &Path,
) -> String {
    let asset_path = path_to_string(asset_path);
    let mut fields = vec![
        ("source_kind", "image".to_string()),
        ("source_location", snapshot.location.clone()),
        ("fetched_at", fetched_at.to_string()),
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
    VisionDegradation::for_routing(
        AiRouting::Daemon,
        "Keep raw image assets and surface filename/metadata only; skip visual extraction.",
    )
}

#[cfg(test)]
mod tests {
    use gobby_core::indexing::content_hash;

    use super::*;
    use crate::sources::{SourceKind, SourceManifest};
    use crate::store::{FakeWikiStore, WikiDocumentKind};

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
        let mut store = FakeWikiStore::default();

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
    fn unchanged_image_reingest_reuses_immutable_raw_capture() {
        let temp = tempfile::tempdir().expect("tempdir");
        let mut store = FakeWikiStore::default();

        let first = ingest_image(
            temp.path(),
            &mut store,
            ScopeIdentity::topic("field-work"),
            sample_snapshot(),
        )
        .expect("first ingest");

        let mut reingest = sample_snapshot();
        reingest.fetched_at = "2026-05-30T09:00:00Z".to_string();
        let second = ingest_image(
            temp.path(),
            &mut store,
            ScopeIdentity::topic("field-work"),
            reingest,
        )
        .expect("unchanged re-ingest");

        assert_eq!(second.record.id, first.record.id);
        assert_eq!(second.raw_path, first.raw_path);
        let raw =
            std::fs::read_to_string(temp.path().join(&second.raw_path)).expect("raw markdown");
        assert!(
            raw.contains("2026-05-29T20:30:00Z"),
            "first capture time kept"
        );
        assert!(
            !raw.contains("2026-05-30T09:00:00Z"),
            "re-ingest time not written"
        );
        let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
        assert_eq!(manifest.entries.len(), 1);
    }

    #[test]
    fn image_metadata_is_scope_indexed() {
        let temp = tempfile::tempdir().expect("tempdir");
        let mut store = FakeWikiStore::default();

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
    fn off_routing_skips_production_vision() {
        let mut context = test_ai_context("http://unused.invalid");
        context.bindings.vision_extract.routing = gobby_core::config::AiRouting::Off;
        let temp = tempfile::tempdir().expect("tempdir");
        let mut store = FakeWikiStore::default();

        let result = ingest_image_with_production_vision(
            temp.path(),
            &mut store,
            ScopeIdentity::topic("field-work"),
            &context,
            sample_snapshot(),
        )
        .expect("ingest image with AI off");
        assert!(result.vision_degradation.is_some());
    }

    #[cfg(feature = "ai")]
    fn test_ai_context(api_base: &str) -> gobby_core::ai_context::AiContext {
        use gobby_core::ai_context::{AiBindings, AiLimiter};
        use gobby_core::config::{AiRouting, AiTuning, CapabilityBinding};

        let binding = CapabilityBinding {
            routing: AiRouting::Daemon,
            transport: None,
            api_base: Some(api_base.to_string()),
            api_key: None,
            model: Some("gpt-4.1-mini".to_string()),
            provider: None,
            task: None,
            language: None,
            target_lang: None,
            profile: None,
            candidates: None,
            reasoning_effort: None,
            verify_profile: None,
            verify_model: None,
            verify_api_key: None,
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
            grant: None,
            tool_loop_limits: gobby_core::ai::generation::ToolLoopLimits::default(),
        }
    }
}
