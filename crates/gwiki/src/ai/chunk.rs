use std::path::PathBuf;
use std::time::Duration;

use crate::WikiError;
use crate::ai::translate;
use crate::transcribe::{
    TranscriptSegment, TranscriptionClient, TranscriptionOutput, TranscriptionRange,
    TranscriptionRequest,
};

pub(crate) const MAX_AUDIO_UPLOAD_BYTES: usize = 24 * 1024 * 1024;
#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
pub(crate) const FIXED_PCM_SAMPLE_RATE_HZ: u64 = 16_000;
#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
pub(crate) const FIXED_PCM_CHANNELS: u64 = 1;
#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
pub(crate) const FIXED_PCM_BYTES_PER_SAMPLE: u64 = 2;
#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
pub(crate) const FIXED_PCM_WAV_HEADER_BYTES: u64 = 44;
pub(crate) const DEFAULT_CHUNK_WINDOW: Duration = Duration::from_secs(10 * 60);
pub(crate) const CHUNK_OVERLAP: Duration = Duration::from_secs(3);

#[derive(Debug, Clone)]
pub(crate) struct AudioChunk {
    pub(crate) start_ms: u64,
    pub(crate) end_ms: u64,
    pub(crate) file_name: String,
    pub(crate) path: PathBuf,
    pub(crate) bytes: Vec<u8>,
}

#[derive(Debug)]
pub(crate) struct ChunkedTranscription {
    pub(crate) output: TranscriptionOutput,
}

#[derive(Debug, Clone, Copy)]
pub(crate) enum ChunkTranscriptionMode<'a> {
    Transcribe,
    TranslateToEnglish {
        language_hint: Option<&'a str>,
    },
    TranslateSegments {
        target_lang: &'a str,
        language_hint: Option<&'a str>,
    },
}

pub(crate) trait AudioChunker {
    fn split(
        &self,
        request: &TranscriptionRequest<'_>,
        window: Duration,
        overlap: Duration,
    ) -> Result<Vec<AudioChunk>, WikiError>;
}

pub(crate) struct MediaAudioChunker;

impl AudioChunker for MediaAudioChunker {
    fn split(
        &self,
        request: &TranscriptionRequest<'_>,
        window: Duration,
        overlap: Duration,
    ) -> Result<Vec<AudioChunk>, WikiError> {
        #[cfg(test)]
        if let Some(chunks) = take_test_chunks() {
            return Ok(chunks);
        }

        crate::media::split_audio_file_with_overlap(request.asset_path, window, overlap)?
            .into_iter()
            .enumerate()
            .map(|(index, chunk)| {
                let bytes = std::fs::read(chunk.path.path()).map_err(|source| WikiError::Io {
                    action: "read audio chunk",
                    path: Some(chunk.path.path().to_path_buf()),
                    source,
                })?;
                Ok(AudioChunk {
                    start_ms: chunk.start_ms,
                    end_ms: chunk.end_ms,
                    file_name: format!("{}.{index:04}.wav", request.file_name),
                    path: chunk.path.path().to_path_buf(),
                    bytes,
                })
            })
            .collect()
    }
}

pub(crate) fn transcribe_audio_request(
    request: &TranscriptionRequest<'_>,
    client: &dyn TranscriptionClient,
    mode: ChunkTranscriptionMode<'_>,
) -> Result<TranscriptionOutput, WikiError> {
    transcribe_audio_request_with_chunker(request, client, mode, &MediaAudioChunker)
}

pub(crate) fn transcribe_audio_request_with_chunker(
    request: &TranscriptionRequest<'_>,
    client: &dyn TranscriptionClient,
    mode: ChunkTranscriptionMode<'_>,
    chunker: &dyn AudioChunker,
) -> Result<TranscriptionOutput, WikiError> {
    if !requires_chunking(request.bytes.len()) {
        return transcribe_single_request(request, client, mode);
    }
    let chunks = chunker.split(request, DEFAULT_CHUNK_WINDOW, CHUNK_OVERLAP)?;
    let chunked = transcribe_chunks(request, client, mode, chunks)?;
    Ok(chunked.output)
}

pub(crate) fn requires_chunking(byte_len: usize) -> bool {
    byte_len > MAX_AUDIO_UPLOAD_BYTES
}

#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
pub(crate) fn fixed_codec_bytes_for_duration(duration: Duration) -> u64 {
    const NANOS_PER_SECOND: u128 = 1_000_000_000;
    let bytes_per_second =
        u128::from(FIXED_PCM_SAMPLE_RATE_HZ * FIXED_PCM_CHANNELS * FIXED_PCM_BYTES_PER_SAMPLE);
    let pcm_bytes = duration
        .as_nanos()
        .saturating_mul(bytes_per_second)
        .saturating_add(NANOS_PER_SECOND - 1)
        / NANOS_PER_SECOND;
    u64::try_from(u128::from(FIXED_PCM_WAV_HEADER_BYTES).saturating_add(pcm_bytes))
        .unwrap_or(u64::MAX)
}

fn transcribe_chunks(
    _original_request: &TranscriptionRequest<'_>,
    client: &dyn TranscriptionClient,
    mode: ChunkTranscriptionMode<'_>,
    mut chunks: Vec<AudioChunk>,
) -> Result<ChunkedTranscription, WikiError> {
    chunks.sort_by_key(|chunk| chunk.start_ms);
    let mut aggregate = empty_output();
    let mut completed_ranges = Vec::new();
    let mut missing_ranges = Vec::new();
    let mut first_error = None;

    for chunk in chunks {
        let request = TranscriptionRequest {
            file_name: &chunk.file_name,
            mime_type: Some("audio/wav"),
            asset_path: &chunk.path,
            bytes: &chunk.bytes,
        };
        match transcribe_chunk_request(&request, client, mode) {
            Ok(mut output) => {
                merge_metadata(&mut aggregate, &output);
                offset_segments(&mut output.segments, chunk.start_ms);
                append_deduped(&mut aggregate.segments, output.segments);
                completed_ranges.push(TranscriptionRange {
                    start_ms: chunk.start_ms,
                    end_ms: chunk.end_ms,
                });
            }
            Err(error) => {
                if first_error.is_none() {
                    first_error = Some(error);
                }
                missing_ranges.push(TranscriptionRange {
                    start_ms: chunk.start_ms,
                    end_ms: chunk.end_ms,
                });
            }
        }
    }

    if completed_ranges.is_empty()
        && let Some(error) = first_error
    {
        return Err(error);
    }

    aggregate.completed_ranges = completed_ranges;
    aggregate.missing_ranges = missing_ranges;
    aggregate.partial = !aggregate.missing_ranges.is_empty();
    if let ChunkTranscriptionMode::TranslateSegments {
        target_lang,
        language_hint,
    } = mode
        && !aggregate.segments.is_empty()
    {
        aggregate = translate::translate_transcription_segments(
            aggregate,
            client,
            target_lang,
            language_hint,
        )?;
    }
    Ok(ChunkedTranscription { output: aggregate })
}

fn transcribe_single_request(
    request: &TranscriptionRequest<'_>,
    client: &dyn TranscriptionClient,
    mode: ChunkTranscriptionMode<'_>,
) -> Result<TranscriptionOutput, WikiError> {
    match mode {
        ChunkTranscriptionMode::Transcribe => client.transcribe(request),
        ChunkTranscriptionMode::TranslateSegments {
            target_lang,
            language_hint,
        } => translate::translate_audio(request, client, Some(target_lang), language_hint),
        ChunkTranscriptionMode::TranslateToEnglish { language_hint } => {
            translate::translate_audio(request, client, Some("en"), language_hint)
        }
    }
}

fn transcribe_chunk_request(
    request: &TranscriptionRequest<'_>,
    client: &dyn TranscriptionClient,
    mode: ChunkTranscriptionMode<'_>,
) -> Result<TranscriptionOutput, WikiError> {
    match mode {
        ChunkTranscriptionMode::Transcribe | ChunkTranscriptionMode::TranslateSegments { .. } => {
            client.transcribe(request)
        }
        ChunkTranscriptionMode::TranslateToEnglish { language_hint } => {
            translate::translate_audio(request, client, Some("en"), language_hint)
        }
    }
}

fn empty_output() -> TranscriptionOutput {
    TranscriptionOutput {
        segments: Vec::new(),
        language: None,
        model: None,
        source_language: None,
        task: None,
        target_language: None,
        translated: false,
        translation_degraded: false,
        partial: false,
        completed_ranges: Vec::new(),
        missing_ranges: Vec::new(),
    }
}

fn merge_metadata(aggregate: &mut TranscriptionOutput, output: &TranscriptionOutput) {
    if aggregate.language.is_none() {
        aggregate.language = output.language.clone();
    }
    if aggregate.model.is_none() {
        aggregate.model = output.model.clone();
    }
    if aggregate.source_language.is_none() {
        aggregate.source_language = output.source_language.clone();
    }
    if aggregate.task.is_none() {
        aggregate.task = output.task.clone();
    }
    if aggregate.target_language.is_none() {
        aggregate.target_language = output.target_language.clone();
    }
    aggregate.translated |= output.translated;
    aggregate.translation_degraded |= output.translation_degraded;
}

fn offset_segments(segments: &mut [TranscriptSegment], chunk_start_ms: u64) {
    for segment in segments {
        segment.start_ms = segment.start_ms.saturating_add(chunk_start_ms);
        segment.end_ms = segment.end_ms.saturating_add(chunk_start_ms);
    }
}

fn append_deduped(segments: &mut Vec<TranscriptSegment>, incoming: Vec<TranscriptSegment>) {
    for segment in incoming {
        if is_overlap_duplicate(segments.last(), &segment) {
            continue;
        }
        segments.push(segment);
    }
}

fn is_overlap_duplicate(previous: Option<&TranscriptSegment>, segment: &TranscriptSegment) -> bool {
    let Some(previous) = previous else {
        return false;
    };
    previous.text.trim() == segment.text.trim()
        && segment.start_ms <= previous.end_ms.saturating_add(duration_ms(CHUNK_OVERLAP))
}

fn duration_ms(duration: Duration) -> u64 {
    duration.as_millis().try_into().unwrap_or(u64::MAX)
}

#[cfg(test)]
thread_local! {
    static TEST_CHUNKS: std::cell::RefCell<Option<Vec<AudioChunk>>> = const { std::cell::RefCell::new(None) };
}

#[cfg(test)]
pub(crate) struct TestChunkGuard;

#[cfg(test)]
impl Drop for TestChunkGuard {
    fn drop(&mut self) {
        TEST_CHUNKS.with(|chunks| {
            chunks.borrow_mut().take();
        });
    }
}

#[cfg(test)]
pub(crate) fn install_test_chunks(chunks: Vec<AudioChunk>) -> TestChunkGuard {
    TEST_CHUNKS.with(|slot| {
        assert!(slot.borrow().is_none(), "test chunks already installed");
        *slot.borrow_mut() = Some(chunks);
    });
    TestChunkGuard
}

#[cfg(test)]
fn take_test_chunks() -> Option<Vec<AudioChunk>> {
    TEST_CHUNKS.with(|chunks| chunks.borrow_mut().take())
}

#[cfg(test)]
mod tests {
    use std::cell::{Cell, RefCell};
    use std::path::Path;

    use super::*;
    use crate::transcribe::{TranscriptSegment, TranscriptionOutput};

    #[test]
    fn chunks_under_limit_fixed_codec() {
        assert_eq!(FIXED_PCM_SAMPLE_RATE_HZ, 16_000);
        assert_eq!(FIXED_PCM_CHANNELS, 1);
        assert_eq!(FIXED_PCM_BYTES_PER_SAMPLE, 2);
        assert!(
            fixed_codec_bytes_for_duration(DEFAULT_CHUNK_WINDOW) < MAX_AUDIO_UPLOAD_BYTES as u64
        );
        assert!(DEFAULT_CHUNK_WINDOW <= Duration::from_secs(12 * 60 + 30));
    }

    #[test]
    fn fixed_codec_bytes_include_subsecond_durations() {
        assert_eq!(
            fixed_codec_bytes_for_duration(Duration::from_millis(500)),
            FIXED_PCM_WAV_HEADER_BYTES + 16_000
        );
    }

    #[test]
    fn offsets_and_dedups() {
        let request = long_request();
        let client = ScriptedClient::new(vec![
            Ok(output(
                "en",
                &[(0, 1_000, "alpha"), (9_500, 10_000, "overlap")],
            )),
            Ok(output("en", &[(0, 500, "overlap"), (500, 1_500, "beta")])),
        ]);
        let chunker = FakeChunker::new(vec![chunk(9_000, 19_000), chunk(0, 10_000)]);

        let stitched = transcribe_audio_request_with_chunker(
            &request,
            &client,
            ChunkTranscriptionMode::Transcribe,
            &chunker,
        )
        .expect("chunk transcription");

        assert_eq!(
            stitched
                .segments
                .iter()
                .map(|segment| (segment.start_ms, segment.end_ms, segment.text.as_str()))
                .collect::<Vec<_>>(),
            vec![
                (0, 1_000, "alpha"),
                (9_500, 10_000, "overlap"),
                (9_500, 10_500, "beta")
            ]
        );
    }

    #[test]
    fn short_audio_bypasses_ffmpeg() {
        let request = short_request();
        let client = ScriptedClient::new(vec![Ok(output("en", &[(0, 1_000, "short")]))]);
        let chunker = FakeChunker::new(Vec::new());

        let output = transcribe_audio_request_with_chunker(
            &request,
            &client,
            ChunkTranscriptionMode::Transcribe,
            &chunker,
        )
        .expect("short transcription");

        assert_eq!(output.segments[0].text, "short");
        assert_eq!(chunker.calls.get(), 0);
    }

    #[test]
    fn short_translate_segments_translates_without_chunking() {
        let request = short_request();
        let client = TranslatingClient {
            transcribe_calls: Cell::new(0),
            translate_calls: Cell::new(0),
        };
        let chunker = FakeChunker::new(Vec::new());

        let output = transcribe_audio_request_with_chunker(
            &request,
            &client,
            ChunkTranscriptionMode::TranslateSegments {
                target_lang: "fr",
                language_hint: Some("es"),
            },
            &chunker,
        )
        .expect("short translated transcription");

        assert_eq!(chunker.calls.get(), 0);
        assert_eq!(client.transcribe_calls.get(), 1);
        assert_eq!(client.translate_calls.get(), 1);
        assert_eq!(output.language.as_deref(), Some("fr"));
        assert_eq!(output.target_language.as_deref(), Some("fr"));
        assert!(output.translated);
        assert_eq!(output.segments[0].text, "bonjour");
    }

    #[test]
    fn partial_chunk_outcome() {
        let request = long_request();
        let client = ScriptedClient::new(vec![
            Ok(output("en", &[(0, 1_000, "kept")])),
            Err(WikiError::Config {
                detail: "provider failed".to_string(),
            }),
            Ok(output("en", &[(0, 1_000, "also kept")])),
        ]);
        let chunker = FakeChunker::new(vec![
            chunk(0, 10_000),
            chunk(9_000, 19_000),
            chunk(18_000, 28_000),
        ]);

        let output = transcribe_audio_request_with_chunker(
            &request,
            &client,
            ChunkTranscriptionMode::Transcribe,
            &chunker,
        )
        .expect("partial transcription");

        assert!(output.partial);
        assert_eq!(
            output.completed_ranges,
            vec![
                TranscriptionRange {
                    start_ms: 0,
                    end_ms: 10_000,
                },
                TranscriptionRange {
                    start_ms: 18_000,
                    end_ms: 28_000,
                },
            ]
        );
        assert_eq!(
            output.missing_ranges,
            vec![TranscriptionRange {
                start_ms: 9_000,
                end_ms: 19_000,
            }]
        );
        assert_eq!(
            output
                .segments
                .iter()
                .map(|segment| segment.text.as_str())
                .collect::<Vec<_>>(),
            vec!["kept", "also kept"]
        );
    }

    struct FakeChunker {
        calls: Cell<usize>,
        chunks: Vec<AudioChunk>,
    }

    impl FakeChunker {
        fn new(chunks: Vec<AudioChunk>) -> Self {
            Self {
                calls: Cell::new(0),
                chunks,
            }
        }
    }

    impl AudioChunker for FakeChunker {
        fn split(
            &self,
            _request: &TranscriptionRequest<'_>,
            _window: Duration,
            _overlap: Duration,
        ) -> Result<Vec<AudioChunk>, WikiError> {
            self.calls.set(self.calls.get() + 1);
            Ok(self.chunks.clone())
        }
    }

    struct ScriptedClient {
        outputs: RefCell<Vec<Result<TranscriptionOutput, WikiError>>>,
    }

    impl ScriptedClient {
        fn new(outputs: Vec<Result<TranscriptionOutput, WikiError>>) -> Self {
            Self {
                outputs: RefCell::new(outputs),
            }
        }
    }

    impl TranscriptionClient for ScriptedClient {
        fn transcribe(
            &self,
            _request: &TranscriptionRequest<'_>,
        ) -> Result<TranscriptionOutput, WikiError> {
            self.outputs.borrow_mut().remove(0)
        }
    }

    struct TranslatingClient {
        transcribe_calls: Cell<usize>,
        translate_calls: Cell<usize>,
    }

    impl TranscriptionClient for TranslatingClient {
        fn transcribe(
            &self,
            _request: &TranscriptionRequest<'_>,
        ) -> Result<TranscriptionOutput, WikiError> {
            self.transcribe_calls.set(self.transcribe_calls.get() + 1);
            Ok(output("es", &[(0, 1_000, "hola")]))
        }

        fn translate_segments(
            &self,
            segments: &[TranscriptSegment],
            source_lang: &str,
            target_lang: &str,
        ) -> Result<Vec<String>, WikiError> {
            self.translate_calls.set(self.translate_calls.get() + 1);
            assert_eq!(source_lang, "es");
            assert_eq!(target_lang, "fr");
            assert_eq!(segments.len(), 1);
            Ok(vec!["bonjour".to_string()])
        }
    }

    fn short_request<'a>() -> TranscriptionRequest<'a> {
        TranscriptionRequest {
            file_name: "short.wav",
            mime_type: Some("audio/wav"),
            asset_path: Path::new("short.wav"),
            bytes: b"short audio",
        }
    }

    #[allow(clippy::large_stack_frames)]
    fn long_request<'a>() -> TranscriptionRequest<'a> {
        // Test-only leak of one bounded oversized fixture. The request model
        // borrows audio bytes, and this avoids repeated >MAX-byte allocations
        // while keeping production allocation paths untouched.
        TranscriptionRequest {
            file_name: "long.wav",
            mime_type: Some("audio/wav"),
            asset_path: Path::new("long.wav"),
            bytes: Box::leak(vec![0; MAX_AUDIO_UPLOAD_BYTES + 1].into_boxed_slice()),
        }
    }

    fn chunk(start_ms: u64, end_ms: u64) -> AudioChunk {
        AudioChunk {
            start_ms,
            end_ms,
            file_name: format!("chunk-{start_ms}.wav"),
            path: PathBuf::from(format!("chunk-{start_ms}.wav")),
            bytes: vec![1, 2, 3],
        }
    }

    fn output(source_lang: &str, segments: &[(u64, u64, &str)]) -> TranscriptionOutput {
        TranscriptionOutput {
            segments: segments
                .iter()
                .map(|(start_ms, end_ms, text)| TranscriptSegment {
                    start_ms: *start_ms,
                    end_ms: *end_ms,
                    text: (*text).to_string(),
                })
                .collect(),
            language: Some(source_lang.to_string()),
            model: Some("fake-stt".to_string()),
            source_language: Some(source_lang.to_string()),
            task: Some("transcribe".to_string()),
            target_language: None,
            translated: false,
            translation_degraded: false,
            partial: false,
            completed_ranges: Vec::new(),
            missing_ranges: Vec::new(),
        }
    }
}
