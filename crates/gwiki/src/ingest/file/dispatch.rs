use std::path::Path;

use gobby_core::ai_context::AiContext;
#[cfg(all(feature = "documents", feature = "ai"))]
use gobby_core::config::AiCapability;
#[cfg(feature = "documents")]
use gobby_core::config::AiRouting;

#[cfg(all(feature = "documents", feature = "ai"))]
use crate::ai::clients::ProductionVisionClient;
use crate::api::IngestFileOptions;
use crate::ingest::audio::{
    AudioSnapshot, ingest_audio_with_transcription_without_index, production_transcription_endpoint,
};
#[cfg(feature = "documents")]
use crate::ingest::document::{DocumentSnapshot, ingest_document_without_index};
use crate::ingest::image::{ImageSnapshot, ingest_image_with_production_vision_without_index};
#[cfg(feature = "documents")]
use crate::ingest::pdf::{
    PdfFileSnapshot, PdfIngestOptions, ingest_pdf_file_without_index, pdf_fetched_at,
};
use crate::ingest::session::{SessionFileSnapshot, ingest_session_file_without_index};
use crate::ingest::video::{
    VideoFileSnapshot, ingest_video_file_with_production_processing_without_index,
};
use crate::sources::SourceKind;
#[cfg(feature = "documents")]
use crate::vision::{VisionDegradation, VisionEndpoint};
use crate::{ScopeIdentity, WikiError};
#[cfg(all(feature = "documents", feature = "ai"))]
use gobby_core::ai::effective_route;

use super::LocalFileIngestResult;
#[cfg(feature = "documents")]
use super::degradation::document_degradation_summary;
use super::degradation::{
    transcription_degradation_summary, video_degradation_summaries, vision_degradation_summary,
};
use super::generic::ingest_generic_file_without_index;
use super::replay::attach_replay_metadata;
use super::source::{detect_source_kind, read_source_file, source_location};

pub(crate) fn ingest_path_without_index(
    vault_root: &Path,
    scope: &ScopeIdentity,
    ai_context: &AiContext,
    options: &IngestFileOptions,
    path: &Path,
    fetched_at: &str,
) -> Result<LocalFileIngestResult, WikiError> {
    let kind = detect_source_kind(path);
    let file_name = path
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("source");
    let location = source_location(vault_root, path);
    let mut local_result = match kind {
        SourceKind::Audio => {
            let bytes = read_source_file(path)?;
            let result = ingest_audio_with_transcription_without_index(
                vault_root,
                scope.clone(),
                AudioSnapshot {
                    location,
                    file_name: file_name.to_string(),
                    fetched_at: fetched_at.to_string(),
                    bytes,
                    mime_type: None,
                    duration_seconds: None,
                },
                production_transcription_endpoint(ai_context, options.translate),
            )?;
            let degradations = result
                .transcription_degradation
                .as_ref()
                .map(transcription_degradation_summary)
                .into_iter()
                .collect();
            LocalFileIngestResult {
                result: result.into(),
                degradations,
            }
        }
        SourceKind::Image => {
            let bytes = read_source_file(path)?;
            let result = ingest_image_with_production_vision_without_index(
                vault_root,
                scope.clone(),
                ai_context,
                ImageSnapshot {
                    location,
                    file_name: file_name.to_string(),
                    fetched_at: fetched_at.to_string(),
                    bytes,
                    mime_type: None,
                    width: None,
                    height: None,
                },
            )?;
            let degradations = result
                .vision_degradation
                .as_ref()
                .map(vision_degradation_summary)
                .into_iter()
                .collect();
            LocalFileIngestResult {
                result: result.into(),
                degradations,
            }
        }
        SourceKind::Video => {
            let result = ingest_video_file_with_production_processing_without_index(
                vault_root,
                scope.clone(),
                ai_context,
                VideoFileSnapshot {
                    location,
                    file_name: file_name.to_string(),
                    fetched_at: fetched_at.to_string(),
                    path: path.to_path_buf(),
                    mime_type: None,
                    duration_seconds: None,
                    frame_interval_seconds: options.video_frame_interval_seconds,
                    frame_samples: Vec::new(),
                    frame_image_paths: Vec::new(),
                    frame_descriptions: Vec::new(),
                    transcript_segments: Vec::new(),
                    transcription: None,
                },
                options.translate,
            )?;
            let degradations = video_degradation_summaries(&result);
            LocalFileIngestResult {
                result: result.into(),
                degradations,
            }
        }
        SourceKind::Session => {
            let bytes = read_source_file(path)?;
            let result = ingest_session_file_without_index(
                vault_root,
                SessionFileSnapshot {
                    location,
                    file_name: file_name.to_string(),
                    fetched_at: fetched_at.to_string(),
                    path: path.to_path_buf(),
                    bytes,
                },
            )?;
            LocalFileIngestResult {
                result,
                degradations: Vec::new(),
            }
        }
        #[cfg(feature = "documents")]
        SourceKind::Pdf => {
            let bytes = read_source_file(path)?;
            let snapshot = PdfFileSnapshot {
                location,
                file_name: file_name.to_string(),
                fetched_at: pdf_fetched_at(fetched_at)?,
                bytes,
            };
            #[cfg(feature = "ai")]
            {
                let client = (effective_route(ai_context, AiCapability::VisionExtract)
                    != AiRouting::Off)
                    .then(|| ProductionVisionClient::new(ai_context.clone()));
                let endpoint = client
                    .as_ref()
                    .map(|client| {
                        VisionEndpoint::Available(client as &dyn crate::vision::VisionClient)
                    })
                    .unwrap_or_else(|| {
                        VisionEndpoint::Unavailable(VisionDegradation::for_routing(
                            ai_context.binding(AiCapability::VisionExtract).routing,
                            "Keep PDF text layer only; skip page raster vision.",
                        ))
                    });
                let result = ingest_pdf_file_without_index(
                    vault_root,
                    scope,
                    snapshot,
                    endpoint,
                    PdfIngestOptions::default(),
                )?;
                LocalFileIngestResult {
                    result,
                    degradations: Vec::new(),
                }
            }
            #[cfg(not(feature = "ai"))]
            {
                let result = ingest_pdf_file_without_index(
                    vault_root,
                    scope,
                    snapshot,
                    VisionEndpoint::Unavailable(VisionDegradation::for_routing(
                        AiRouting::Off,
                        "Keep PDF text layer only; skip page raster vision.",
                    )),
                    PdfIngestOptions::default(),
                )?;
                LocalFileIngestResult {
                    result,
                    degradations: Vec::new(),
                }
            }
        }
        #[cfg(feature = "documents")]
        SourceKind::Office | SourceKind::Html => {
            let bytes = read_source_file(path)?;
            let result = ingest_document_without_index(
                vault_root,
                scope.clone(),
                DocumentSnapshot {
                    location,
                    file_name: file_name.to_string(),
                    fetched_at: fetched_at.to_string(),
                    bytes,
                    kind,
                },
            )?;
            let degradations = result
                .document_degradation
                .as_ref()
                .map(document_degradation_summary)
                .into_iter()
                .collect();
            LocalFileIngestResult {
                result: result.into(),
                degradations,
            }
        }
        _ => ingest_generic_file_without_index(
            vault_root, &kind, file_name, &location, path, fetched_at,
        )?,
    };

    attach_replay_metadata(vault_root, &mut local_result.result, path, options)?;
    Ok(local_result)
}
