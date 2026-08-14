use std::fs;
use std::path::{Path, PathBuf};

use gobby_core::ai_context::AiContext;
use gobby_core::config::{AiCapability, AiRouting};

#[cfg(feature = "ai")]
use gobby_core::ai::generation::{
    DirectGenerationTarget, GenerationTier, profile_for_tier, resolve_direct_generation_target,
};

#[cfg(feature = "ai")]
use crate::ai::clients::ProductionTranscriptionClient;
use crate::ingest::{
    IngestResult, existing_raw_markdown, index_after_ingest, markdown_metadata, markdown_title,
    path_to_string, write_asset, write_raw_markdown,
};
use crate::sources::{SourceDraft, SourceKind, SourceManifest};
use crate::store::WikiIndexStore;
use crate::transcribe::{
    TranscriptionDegradation, TranscriptionEndpoint, TranscriptionMarkdownInput,
    TranscriptionRequest, write_audio_transcript_markdown,
};
use crate::{ScopeIdentity, WikiError};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AudioSnapshot {
    pub location: String,
    pub file_name: String,
    pub fetched_at: String,
    pub bytes: Vec<u8>,
    pub mime_type: Option<String>,
    pub duration_seconds: Option<u32>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AudioIngestResult {
    pub record: crate::sources::SourceRecord,
    pub raw_path: PathBuf,
    pub asset_path: PathBuf,
    pub transcript_path: PathBuf,
    pub transcription_degradation: Option<TranscriptionDegradation>,
}

#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
pub fn ingest_audio(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    scope: ScopeIdentity,
    snapshot: AudioSnapshot,
    ai_context: &AiContext,
) -> Result<AudioIngestResult, WikiError> {
    ingest_audio_with_transcription(
        vault_root,
        store,
        scope,
        snapshot,
        production_transcription_endpoint(ai_context, false),
    )
}

pub fn production_transcription_endpoint(
    context: &AiContext,
    translate: bool,
) -> TranscriptionEndpoint<'static> {
    let capability = if translate {
        AiCapability::AudioTranslate
    } else {
        AiCapability::AudioTranscribe
    };
    let route = resolved_transcription_route(context, capability);
    if translate {
        let transcribe_route = resolved_transcription_route(context, AiCapability::AudioTranscribe);
        let text_route = resolved_transcription_route(context, AiCapability::TextGenerate);
        if route_available(route)
            || (route_available(transcribe_route) && route_available(text_route))
        {
            available_production_transcription_endpoint(context, route, translate)
        } else {
            TranscriptionEndpoint::Unavailable(TranscriptionDegradation::for_routing(
                route,
                transcription_fallback(translate),
            ))
        }
    } else if matches!(route, AiRouting::Daemon) {
        available_production_transcription_endpoint(context, route, translate)
    } else {
        TranscriptionEndpoint::Unavailable(TranscriptionDegradation::for_routing(
            route,
            transcription_fallback(translate),
        ))
    }
}

fn route_available(route: AiRouting) -> bool {
    matches!(route, AiRouting::Daemon)
}

#[cfg(feature = "ai")]
fn resolved_transcription_route(context: &AiContext, capability: AiCapability) -> AiRouting {
    gobby_core::ai::effective_route(context, capability)
}

#[cfg(not(feature = "ai"))]
fn resolved_transcription_route(context: &AiContext, capability: AiCapability) -> AiRouting {
    context.binding(capability).routing
}

#[cfg(feature = "ai")]
fn available_production_transcription_endpoint(
    context: &AiContext,
    _route: AiRouting,
    translate: bool,
) -> TranscriptionEndpoint<'static> {
    // Translation runs through the Standard (`feature_low`) text-generate tier;
    // resolve its Direct-route target once here so the client can route both
    // transports through the shared tier->profile mapping. Pure transcription
    // never touches text generation, so it carries no target.
    let text_generate_target = if translate {
        text_generate_translation_target(context)
    } else {
        None
    };
    let client = Box::new(ProductionTranscriptionClient::new(
        context.clone(),
        text_generate_target,
    ));
    if translate {
        TranscriptionEndpoint::Translating {
            client,
            target_lang: context
                .binding(AiCapability::AudioTranslate)
                .target_lang
                .clone(),
            language_hint: context
                .binding(AiCapability::AudioTranscribe)
                .language
                .clone(),
        }
    } else {
        TranscriptionEndpoint::Available(client)
    }
}

/// Resolve the Standard (`feature_low`) text-generate tier target for segment
/// translation. Only the Direct route consumes a per-tier target (the Daemon
/// route forwards the profile name), so this returns `None` unless text
/// generation resolves to Direct, and `None` when the hub config source cannot
/// be resolved.
#[cfg(feature = "ai")]
fn text_generate_translation_target(context: &AiContext) -> Option<DirectGenerationTarget> {
    if !matches!(
        resolved_transcription_route(context, AiCapability::TextGenerate),
        AiRouting::Daemon
    ) {
        return None;
    }
    let mut source =
        crate::support::config::hub_ai_config_source("gwiki transcription translate").ok()?;
    Some(resolve_direct_generation_target(
        &mut source,
        &profile_for_tier(GenerationTier::Standard, None),
    ))
}

#[cfg(not(feature = "ai"))]
fn available_production_transcription_endpoint(
    _context: &AiContext,
    route: AiRouting,
    translate: bool,
) -> TranscriptionEndpoint<'static> {
    TranscriptionEndpoint::Unavailable(TranscriptionDegradation::for_routing(
        route,
        transcription_fallback(translate),
    ))
}

fn transcription_fallback(translate: bool) -> &'static str {
    if translate {
        "Keep raw audio assets and skip daemon translation."
    } else {
        "Keep raw audio assets and skip daemon transcription."
    }
}

#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
pub fn ingest_audio_with_transcription(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    scope: ScopeIdentity,
    snapshot: AudioSnapshot,
    endpoint: TranscriptionEndpoint<'_>,
) -> Result<AudioIngestResult, WikiError> {
    let result =
        ingest_audio_with_transcription_without_index(vault_root, scope, snapshot, endpoint)?;
    index_after_ingest(
        vault_root,
        store,
        &mut crate::progress::ProgressOptions::default(),
    )?;
    Ok(result)
}

pub(crate) fn ingest_audio_with_transcription_without_index(
    vault_root: &Path,
    scope: ScopeIdentity,
    snapshot: AudioSnapshot,
    endpoint: TranscriptionEndpoint<'_>,
) -> Result<AudioIngestResult, WikiError> {
    let title = markdown_title(&snapshot.file_name);
    let content_hash = gobby_core::indexing::content_hash(&snapshot.bytes);
    let draft = SourceDraft::new(
        snapshot.location.clone(),
        SourceKind::Audio,
        snapshot.fetched_at.clone(),
        Vec::new(),
    )
    .with_title(title)
    .with_citation(snapshot.location.clone());
    let record = SourceManifest::register_with_content_hash(vault_root, draft, content_hash)?;
    let asset_path = write_asset(vault_root, &record, &snapshot.file_name, &snapshot.bytes)?;
    // Reuse the first capture on unchanged re-ingest; fresh writes render with
    // the record's stored capture time so recovery re-writes stay
    // byte-identical to the manifest record (#17650).
    let raw_path = match existing_raw_markdown(vault_root, &record) {
        Some(existing) => existing,
        None => {
            let raw_markdown = render_raw_audio_markdown(
                &snapshot,
                &record.fetched_at,
                &record.content_hash,
                &asset_path,
            );
            match write_raw_markdown(vault_root, &record, &raw_markdown) {
                Ok(raw_path) => raw_path,
                Err(error) => {
                    let asset_full_path = vault_root.join(&asset_path);
                    if let Err(cleanup_error) = fs::remove_file(&asset_full_path)
                        && cleanup_error.kind() != std::io::ErrorKind::NotFound
                    {
                        log::warn!(
                            "failed to clean up audio asset {} after raw markdown write failed: {}",
                            asset_full_path.display(),
                            cleanup_error
                        );
                    }
                    return Err(error);
                }
            }
        }
    };
    let request = TranscriptionRequest {
        file_name: &snapshot.file_name,
        mime_type: snapshot.mime_type.as_deref(),
        asset_path: &asset_path,
        bytes: &snapshot.bytes,
    };
    let transcript = write_audio_transcript_markdown(
        vault_root,
        &scope,
        &record,
        request,
        transcribe_for_markdown(&request, endpoint),
    )?;

    Ok(AudioIngestResult {
        record,
        raw_path,
        asset_path,
        transcript_path: transcript.path,
        transcription_degradation: transcript.degradation,
    })
}

pub(crate) fn transcribe_for_markdown(
    request: &TranscriptionRequest<'_>,
    endpoint: TranscriptionEndpoint<'_>,
) -> TranscriptionMarkdownInput {
    match endpoint {
        TranscriptionEndpoint::Available(client) => {
            transcription_result_for_markdown(request, client.as_ref())
        }
        TranscriptionEndpoint::Unavailable(degradation) => {
            TranscriptionMarkdownInput::Degraded(degradation)
        }
        TranscriptionEndpoint::Translating {
            client,
            target_lang,
            language_hint,
        } => translate_for_markdown(
            request,
            client.as_ref(),
            target_lang.as_deref(),
            language_hint.as_deref(),
        ),
    }
}

fn transcription_result_for_markdown(
    request: &TranscriptionRequest<'_>,
    client: &dyn crate::transcribe::TranscriptionClient,
) -> TranscriptionMarkdownInput {
    let result = transcribe_available(request, client);
    transcription_result_to_markdown(
        result,
        gobby_core::degradation::ModalityDegradationReason::TranscriptionError,
        "Transcription failed",
    )
}

#[cfg(feature = "ai")]
fn transcribe_available(
    request: &TranscriptionRequest<'_>,
    client: &dyn crate::transcribe::TranscriptionClient,
) -> Result<crate::transcribe::TranscriptionOutput, WikiError> {
    crate::ai::chunk::transcribe_audio_request(
        request,
        client,
        crate::ai::chunk::ChunkTranscriptionMode::Transcribe,
    )
}

#[cfg(not(feature = "ai"))]
fn transcribe_available(
    request: &TranscriptionRequest<'_>,
    client: &dyn crate::transcribe::TranscriptionClient,
) -> Result<crate::transcribe::TranscriptionOutput, WikiError> {
    client.transcribe(request)
}

#[cfg(feature = "ai")]
fn translate_for_markdown(
    request: &TranscriptionRequest<'_>,
    client: &dyn crate::transcribe::TranscriptionClient,
    target_lang: Option<&str>,
    language_hint: Option<&str>,
) -> TranscriptionMarkdownInput {
    let result = if crate::ai::chunk::requires_chunking(request.bytes.len()) {
        let target_lang = target_lang.unwrap_or("en");
        let mode = if is_english_target(target_lang) {
            crate::ai::chunk::ChunkTranscriptionMode::TranslateToEnglish { language_hint }
        } else {
            crate::ai::chunk::ChunkTranscriptionMode::TranslateSegments {
                target_lang,
                language_hint,
            }
        };
        crate::ai::chunk::transcribe_audio_request(request, client, mode)
    } else {
        crate::ai::translate::translate_audio(request, client, target_lang, language_hint)
    };
    transcription_result_to_markdown(
        result,
        gobby_core::degradation::ModalityDegradationReason::TranslationError,
        "Translation failed",
    )
}

#[cfg(not(feature = "ai"))]
fn translate_for_markdown(
    _request: &TranscriptionRequest<'_>,
    _client: &dyn crate::transcribe::TranscriptionClient,
    _target_lang: Option<&str>,
    _language_hint: Option<&str>,
) -> TranscriptionMarkdownInput {
    TranscriptionMarkdownInput::Degraded(TranscriptionDegradation {
        reason: gobby_core::degradation::ModalityDegradationReason::TranslationUnavailable,
        fallback: "Translation requires the ai feature.".to_string(),
    })
}

fn transcription_result_to_markdown(
    result: Result<crate::transcribe::TranscriptionOutput, WikiError>,
    reason: gobby_core::degradation::ModalityDegradationReason,
    prefix: &str,
) -> TranscriptionMarkdownInput {
    match result {
        Ok(output) if crate::transcribe::transcription_output_is_empty(&output) => {
            // The daemon can answer HTTP 200 with no speech segments (e.g. the
            // STT engine is unavailable). Degrade rather than record a
            // successful transcript with an empty body.
            TranscriptionMarkdownInput::Degraded(TranscriptionDegradation {
                reason,
                fallback: format!(
                    "{prefix}: transcription returned no speech segments; keep raw audio assets and require supplied transcripts."
                ),
            })
        }
        Ok(output) => TranscriptionMarkdownInput::Transcribed(output),
        Err(error) => TranscriptionMarkdownInput::Degraded(TranscriptionDegradation {
            reason,
            fallback: format!(
                "{prefix}: {error}; keep raw audio assets and require supplied transcripts."
            ),
        }),
    }
}

#[cfg(feature = "ai")]
fn is_english_target(target_lang: &str) -> bool {
    target_lang
        .trim()
        .split(['-', '_'])
        .next()
        .unwrap_or("")
        .eq_ignore_ascii_case("en")
}

impl From<AudioIngestResult> for IngestResult {
    fn from(result: AudioIngestResult) -> Self {
        Self {
            record: result.record,
            raw_path: result.raw_path,
            asset_path: Some(result.asset_path),
        }
    }
}

fn render_raw_audio_markdown(
    snapshot: &AudioSnapshot,
    fetched_at: &str,
    source_hash: &str,
    asset_path: &Path,
) -> String {
    let asset_path = path_to_string(asset_path);
    let mut fields = vec![
        ("source_kind", "audio".to_string()),
        ("source_location", snapshot.location.clone()),
        ("fetched_at", fetched_at.to_string()),
        ("source_hash", source_hash.to_string()),
        ("source_asset", asset_path.clone()),
    ];
    if let Some(mime_type) = &snapshot.mime_type {
        fields.push(("audio_mime_type", mime_type.clone()));
    }
    if let Some(duration_seconds) = snapshot.duration_seconds {
        fields.push(("audio_duration_seconds", duration_seconds.to_string()));
    }

    let mut markdown = markdown_metadata(&fields);
    markdown.push_str("# ");
    markdown.push_str(&markdown_title(&snapshot.file_name));
    markdown.push_str("\n\n");
    markdown.push_str("Original audio stored under `");
    markdown.push_str(&asset_path);
    markdown.push_str("`.\n");
    markdown
}

#[cfg(test)]
mod tests;
