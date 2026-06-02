use crate::WikiError;
use crate::transcribe::{
    TranscriptSegment, TranscriptionClient, TranscriptionOutput, TranscriptionRequest,
};

pub(crate) fn translate_audio(
    request: &TranscriptionRequest<'_>,
    client: &dyn TranscriptionClient,
    target_lang: Option<&str>,
    language_hint: Option<&str>,
) -> Result<TranscriptionOutput, WikiError> {
    let target_lang = normalized_lang(target_lang).unwrap_or_else(|| "en".to_string());
    if is_english(&target_lang) {
        return match client.translate_to_english(request, language_hint) {
            Ok(output) => Ok(mark_english_translation(output, language_hint)),
            Err(_) => {
                let output = client.transcribe(request)?;
                translate_transcription_segments(output, client, &target_lang, language_hint)
            }
        };
    }

    let output = client.transcribe(request)?;
    translate_transcription_segments(output, client, &target_lang, language_hint)
}

pub(crate) fn translate_transcription_segments(
    mut output: TranscriptionOutput,
    client: &dyn TranscriptionClient,
    target_lang: &str,
    language_hint: Option<&str>,
) -> Result<TranscriptionOutput, WikiError> {
    let source_lang = source_language(&output, language_hint)?;
    output.source_language = Some(source_lang.clone());

    if same_lang(&source_lang, target_lang) {
        output.translated = false;
        return Ok(output);
    }

    let translated_texts =
        translate_segment_texts(client, &output.segments, &source_lang, target_lang)?;
    for (segment, translated_text) in output.segments.iter_mut().zip(translated_texts) {
        segment.text = translated_text;
    }
    output.language = Some(target_lang.to_string());
    output.target_language = Some(target_lang.to_string());
    output.task = Some("translate".to_string());
    output.translated = true;
    Ok(output)
}

fn translate_segment_texts(
    client: &dyn TranscriptionClient,
    segments: &[TranscriptSegment],
    source_lang: &str,
    target_lang: &str,
) -> Result<Vec<String>, WikiError> {
    match client.translate_segments(segments, source_lang, target_lang) {
        Ok(first) if first.len() == segments.len() => return Ok(first),
        Ok(first) => warn_translation_batch_mismatch("first", first.len(), segments.len()),
        Err(error) => warn_translation_batch_error("first", &error),
    }

    let second = client.translate_segments(segments, source_lang, target_lang)?;
    if second.len() == segments.len() {
        return Ok(second);
    }
    warn_translation_batch_mismatch("second", second.len(), segments.len());

    segments
        .iter()
        .map(|segment| {
            client
                .translate_segments(std::slice::from_ref(segment), source_lang, target_lang)
                .and_then(|texts| {
                    texts.into_iter().next().ok_or_else(|| WikiError::Config {
                        detail: "segment translation returned no text".to_string(),
                    })
                })
        })
        .collect()
}

fn warn_translation_batch_mismatch(attempt: &str, actual_len: usize, expected_len: usize) {
    eprintln!(
        "Warning: {attempt} translation batch returned {actual_len} text(s) for {expected_len} segment(s); retrying or falling back to smaller batches"
    );
}

fn warn_translation_batch_error(attempt: &str, error: &WikiError) {
    eprintln!("Warning: {attempt} translation batch failed; retrying whole batch: {error}");
}

fn mark_english_translation(
    mut output: TranscriptionOutput,
    language_hint: Option<&str>,
) -> TranscriptionOutput {
    let source_lang = source_language(&output, language_hint).ok();
    output.source_language = source_lang;
    output.language = Some("en".to_string());
    output.target_language = Some("en".to_string());
    output.task = Some("translate".to_string());
    output.translated = true;
    output
}

fn source_language(
    output: &TranscriptionOutput,
    language_hint: Option<&str>,
) -> Result<String, WikiError> {
    normalized_lang(language_hint)
        .or_else(|| normalized_lang(output.source_language.as_deref()))
        .or_else(|| normalized_lang(output.language.as_deref()))
        .ok_or_else(|| WikiError::Config {
            detail: "transcription did not report a source language".to_string(),
        })
}

fn normalized_lang(language: Option<&str>) -> Option<String> {
    language
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(|value| value.to_ascii_lowercase())
}

fn is_english(language: &str) -> bool {
    same_lang(language, "en") || language.starts_with("en-") || language.starts_with("en_")
}

fn same_lang(left: &str, right: &str) -> bool {
    left.eq_ignore_ascii_case(right)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::transcribe::{TranscriptSegment, TranscriptionOutput};
    use std::cell::RefCell;
    use std::path::Path;

    #[derive(Default)]
    struct FakeTranslationClient {
        transcript: RefCell<Option<TranscriptionOutput>>,
        english: RefCell<Option<Result<TranscriptionOutput, WikiError>>>,
        segment_batches: RefCell<Vec<(Vec<TranscriptSegment>, String, String)>>,
        segment_errors: RefCell<Vec<WikiError>>,
        translated_texts: RefCell<Vec<Vec<String>>>,
        calls: RefCell<Vec<&'static str>>,
    }

    impl FakeTranslationClient {
        fn with_transcript(output: TranscriptionOutput) -> Self {
            Self {
                transcript: RefCell::new(Some(output)),
                ..Self::default()
            }
        }

        fn with_english(output: Result<TranscriptionOutput, WikiError>) -> Self {
            Self {
                english: RefCell::new(Some(output)),
                ..Self::default()
            }
        }
    }

    impl TranscriptionClient for FakeTranslationClient {
        fn transcribe(
            &self,
            _request: &TranscriptionRequest<'_>,
        ) -> Result<TranscriptionOutput, WikiError> {
            self.calls.borrow_mut().push("transcribe");
            Ok(self.transcript.borrow_mut().take().unwrap())
        }

        fn translate_to_english(
            &self,
            _request: &TranscriptionRequest<'_>,
            _language_hint: Option<&str>,
        ) -> Result<TranscriptionOutput, WikiError> {
            self.calls.borrow_mut().push("translate_to_english");
            self.english.borrow_mut().take().unwrap()
        }

        fn translate_segments(
            &self,
            segments: &[TranscriptSegment],
            source_lang: &str,
            target_lang: &str,
        ) -> Result<Vec<String>, WikiError> {
            self.calls.borrow_mut().push("translate_segments");
            self.segment_batches.borrow_mut().push((
                segments.to_vec(),
                source_lang.to_string(),
                target_lang.to_string(),
            ));
            if !self.segment_errors.borrow().is_empty() {
                return Err(self.segment_errors.borrow_mut().remove(0));
            }
            Ok(self.translated_texts.borrow_mut().remove(0))
        }
    }

    #[test]
    fn precedence_and_segment_wise() {
        let request = request();
        let same_target = FakeTranslationClient::with_transcript(output("es", &["hola"]));

        let skipped = translate_audio(&request, &same_target, Some("es"), None).unwrap();

        assert!(!skipped.translated);
        assert!(same_target.segment_batches.borrow().is_empty());

        let client = FakeTranslationClient::with_transcript(output("fr", &["bonjour", "monde"]));
        client
            .translated_texts
            .borrow_mut()
            .push(vec!["guten tag".to_string(), "welt".to_string()]);

        let translated = translate_audio(&request, &client, Some("de"), None).unwrap();

        assert_eq!(translated.target_language.as_deref(), Some("de"));
        assert_eq!(translated.source_language.as_deref(), Some("fr"));
        assert!(translated.translated);
        assert_eq!(translated.segments[0].start_ms, 0);
        assert_eq!(translated.segments[0].end_ms, 1000);
        assert_eq!(translated.segments[0].text, "guten tag");
        assert_eq!(translated.segments[1].start_ms, 1000);
        assert_eq!(translated.segments[1].end_ms, 2000);
        assert_eq!(translated.segments[1].text, "welt");
    }

    #[test]
    fn translations_unsupported_fallback() {
        let request = request();
        let client = FakeTranslationClient::with_english(Err(WikiError::Config {
            detail: "audio translations endpoint unsupported".to_string(),
        }));
        *client.transcript.borrow_mut() = Some(output("es", &["hola"]));
        client
            .translated_texts
            .borrow_mut()
            .push(vec!["hello".to_string()]);

        let translated = translate_audio(&request, &client, None, None).unwrap();

        assert_eq!(
            client.calls.borrow().as_slice(),
            &["translate_to_english", "transcribe", "translate_segments"]
        );
        assert_eq!(translated.target_language.as_deref(), Some("en"));
        assert_eq!(translated.segments[0].text, "hello");
    }

    #[test]
    fn english_one_pass_vs_target_first() {
        let request = request();
        let english_client = FakeTranslationClient::with_english(Ok(output("es", &["hello"])));

        translate_audio(&request, &english_client, Some("en"), None).unwrap();

        assert_eq!(
            english_client.calls.borrow().as_slice(),
            &["translate_to_english"]
        );

        let non_english_client = FakeTranslationClient::with_transcript(output("es", &["hola"]));
        non_english_client
            .translated_texts
            .borrow_mut()
            .push(vec!["bonjour".to_string()]);
        translate_audio(&request, &non_english_client, Some("fr"), None).unwrap();

        assert_eq!(
            non_english_client.calls.borrow().first().copied(),
            Some("transcribe")
        );
        assert!(
            !non_english_client
                .calls
                .borrow()
                .contains(&"translate_to_english")
        );
    }

    #[test]
    fn batch_translation_errors_retry_batch_before_success() {
        let request = request();
        let client = FakeTranslationClient::with_transcript(output("fr", &["bonjour", "monde"]));
        client.segment_errors.borrow_mut().push(WikiError::Config {
            detail: "batch unavailable".to_string(),
        });
        client
            .translated_texts
            .borrow_mut()
            .push(vec!["hello".to_string(), "world".to_string()]);

        let translated = translate_audio(&request, &client, Some("de"), None).unwrap();

        assert_eq!(translated.segments[0].text, "hello");
        assert_eq!(translated.segments[1].text, "world");
        assert_eq!(client.segment_batches.borrow().len(), 2);
        assert!(
            client
                .segment_batches
                .borrow()
                .iter()
                .all(|(segments, _, _)| { segments.len() == 2 })
        );
    }

    fn request<'a>() -> TranscriptionRequest<'a> {
        TranscriptionRequest {
            file_name: "clip.webm",
            mime_type: Some("audio/webm"),
            asset_path: Path::new("clip.webm"),
            bytes: b"audio",
        }
    }

    fn output(source_lang: &str, texts: &[&str]) -> TranscriptionOutput {
        TranscriptionOutput {
            segments: texts
                .iter()
                .enumerate()
                .map(|(index, text)| TranscriptSegment {
                    start_ms: (index as u64) * 1000,
                    end_ms: (index as u64 + 1) * 1000,
                    text: (*text).to_string(),
                })
                .collect(),
            language: Some(source_lang.to_string()),
            model: Some("test-model".to_string()),
            source_language: Some(source_lang.to_string()),
            task: Some("transcribe".to_string()),
            target_language: None,
            translated: false,
            partial: false,
            completed_ranges: Vec::new(),
            missing_ranges: Vec::new(),
        }
    }
}
