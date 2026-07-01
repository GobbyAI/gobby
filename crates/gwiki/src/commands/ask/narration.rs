/// Maximum leading sentences scanned for a narration preamble. Live escapes
/// run well under this (the #720 sample is 17 sentences); the cap bounds the
/// strip window so a long answer can never lose deep content to a stray
/// late first-person sentence.
const NARRATION_SCAN_LIMIT: usize = 30;

pub(super) fn strip_leading_model_narration(answer: &str) -> String {
    let original = answer.trim_start();

    // (offset into `original` where the next sentence starts, is_narration)
    let mut spans: Vec<(usize, bool)> = Vec::new();
    let mut cursor = 0usize;
    while spans.len() < NARRATION_SCAN_LIMIT {
        let rest = &original[cursor..];
        let Some(end) = leading_sentence_end(rest) else {
            break;
        };
        let narration = is_model_narration_sentence(rest[..end].trim());
        cursor += end;
        cursor = original.len() - original[cursor..].trim_start().len();
        spans.push((cursor, narration));
    }

    // Only an answer that *opens* with narration is a candidate; a content
    // opener means any later first-person sentence is part of the answer.
    if !spans.first().is_some_and(|&(_, narration)| narration) {
        return original.to_string();
    }

    // Contiguous narration run (the conservative #702 behavior).
    let mut cut = spans
        .iter()
        .take_while(|&&(_, narration)| narration)
        .count();

    // A narration preamble can interleave declarative asides ("The repo root
    // is not the source tree I expected."). Extend the cut to the last
    // narration sentence when narration still dominates the skipped region —
    // a real answer after the run dilutes the density and blocks extension.
    if let Some(last) = spans.iter().rposition(|&(_, narration)| narration)
        && last + 1 > cut
    {
        let narration_count = spans[..=last]
            .iter()
            .filter(|&&(_, narration)| narration)
            .count();
        if narration_count * 3 >= (last + 1) * 2 {
            cut = last + 1;
        }
    }

    let remaining = &original[spans[cut - 1].0..];
    if remaining.is_empty() {
        original.to_string()
    } else {
        remaining.to_string()
    }
}

fn leading_sentence_end(text: &str) -> Option<usize> {
    text.char_indices()
        .find(|(_, ch)| matches!(ch, '.' | '!' | '?'))
        .map(|(index, ch)| index + ch.len_utf8())
}

/// Discourse markers that may precede a first-person narration opener
/// ("First I'll inspect…", "Next, I'm reading…"). Stripped, with an optional
/// comma, before the opener check.
const NARRATION_DISCOURSE_MARKERS: &[&str] = &[
    "first", "next", "now", "then", "so", "ok", "okay", "alright", "finally",
];

/// First-person openers that signal the model is talking about itself rather
/// than answering. Checked after discourse markers are stripped.
const NARRATION_OPENERS: &[&str] = &[
    "i'm ", "i am ", "i'll ", "i will ", "i've ", "i have ", "i found ", "i need ", "i want ",
    "i still ", "i just ", "let me ", "we're ", "we'll ",
];

/// Process vocabulary that distinguishes investigation narration ("I'm
/// pulling the exact lines now.") from a first-person sentence that could be
/// answer content. Matched anywhere in the normalized sentence.
const NARRATION_PROCESS_MARKERS: &[&str] = &[
    "check", "look", "review", "read", "summariz", "answer", "question", "docs", "document",
    "provided", "evidence", "retriev", "wiki", "inspect", "confirm", "verif", "pull", "grab",
    "search", "scan", "fall", "dig", "examin", "found", "code", "pass", "context", "layout",
];

fn is_model_narration_sentence(sentence: &str) -> bool {
    let normalized = sentence
        .trim()
        .replace(['\u{2018}', '\u{2019}'], "'")
        .to_ascii_lowercase();
    let opener = strip_narration_discourse_markers(&normalized);
    let starts_like_narration = NARRATION_OPENERS
        .iter()
        .any(|prefix| opener.starts_with(prefix));
    let describes_process = NARRATION_PROCESS_MARKERS
        .iter()
        .any(|marker| normalized.contains(marker));

    starts_like_narration && describes_process
}

fn strip_narration_discourse_markers(normalized: &str) -> &str {
    let mut rest = normalized;
    loop {
        let mut advanced = false;
        for marker in NARRATION_DISCOURSE_MARKERS {
            if let Some(tail) = rest.strip_prefix(marker) {
                let tail = tail.strip_prefix(',').unwrap_or(tail);
                if let Some(tail) = tail.strip_prefix(' ') {
                    rest = tail.trim_start();
                    advanced = true;
                    break;
                }
            }
        }
        if !advanced {
            return rest;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn interleaved_narration_preamble_is_stripped_to_answer() {
        let answer = concat!(
            "First I\u{2019}ll inspect the transport and failure-handling code around ",
            "inbox/quarantine.`gcode` can\u{2019}t reach the local hub in this environment, ",
            "so I\u{2019}m falling back to direct source reads. I\u{2019}m looking for the ",
            "exact branch that handles enqueue failures and whether it exits, quarantines, ",
            "or silently degrades.The repo root in this workspace is not the raw crate ",
            "source tree I expected. I\u{2019}m checking the actual layout so I can read the ",
            "right files instead of guessing paths from the wiki index.I found the ",
            "daemon-side inbox replay code. Next I\u{2019}m reading the quarantine and replay ",
            "branches plus the regression tests so I can tell you whether a failed enqueue ",
            "drops, quarantines, or retries the envelope.The daemon replay side is clear: ",
            "failures there are retried later, while malformed envelopes are quarantined. ",
            "I still need to confirm the client-side ghook enqueue path, because ",
            "that\u{2019}s the more likely reading of your question.I\u{2019}ve confirmed the ",
            "daemon replay semantics. Now I\u{2019}m checking the enqueue-first contract in ",
            "the docs, because that\u{2019}s where the client-side failure mode is ",
            "specified.I have enough context to answer, but I want line-accurate references ",
            "for the two distinct failure modes: client enqueue/post failure versus daemon ",
            "replay failure. I\u{2019}m pulling the exact lines now.I\u{2019}ve got the code ",
            "path. I\u{2019}m doing one last pass for a memory hook, because this repo asks ",
            "agents to persist durable codebase facts when they learn one.I found the key ",
            "implementation gap: the current daemon code keeps failed replay files in the ",
            "inbox, while the planning docs describe a future backoff/quarantine policy. ",
            "I\u{2019}m grabbing the regression test line numbers to back that up precisely.",
            "The current code path does not drop a replayable inbox file on failure.",
        );

        assert_eq!(
            strip_leading_model_narration(answer),
            "The current code path does not drop a replayable inbox file on failure."
        );
    }

    #[test]
    fn content_opener_disables_narration_stripping() {
        let answer = "Hooks run at turn boundaries. The envelope is enqueued before the POST. \
                          I'm checking the wiki docs for details.";
        assert_eq!(strip_leading_model_narration(answer), answer);
    }

    #[test]
    fn low_narration_density_strips_only_the_leading_run() {
        let answer = "I'm checking the wiki docs before answering. Hooks run at turn \
                          boundaries. Envelopes are enqueued before the daemon POST. Failures \
                          leave the file in place for the next drain pass. Malformed envelopes \
                          are quarantined with a sidecar. I'm reading the contract docs for the \
                          exact field names.";
        let stripped = strip_leading_model_narration(answer);
        assert!(stripped.starts_with("Hooks run at turn boundaries."));
        assert!(stripped.ends_with("I'm reading the contract docs for the exact field names."));
    }

    #[test]
    fn all_narration_answer_is_kept_verbatim() {
        let answer = "I'm checking the wiki docs now.";
        assert_eq!(strip_leading_model_narration(answer), answer);
    }

    #[test]
    fn discourse_markers_before_first_person_openers_are_narration() {
        for sentence in [
            "First I\u{2019}ll inspect the transport and failure-handling code.",
            "Next I\u{2019}m reading the quarantine and replay branches.",
            "Now, let me check the enqueue contract.",
            "I found the daemon-side inbox replay code.",
            "I still need to confirm the client-side ghook enqueue path.",
        ] {
            assert!(
                is_model_narration_sentence(sentence),
                "expected narration: {sentence}"
            );
        }
        for sentence in [
            "The daemon retries failed replays later.",
            "First, configure the daemon endpoint.",
            "I expected the inbox to drop the file.",
            "Failures leave the file in place for the next drain pass.",
        ] {
            assert!(
                !is_model_narration_sentence(sentence),
                "expected content: {sentence}"
            );
        }
    }
}
