//! Standalone session-summary generation for `gwiki sync-sessions --summarize`.
//!
//! When no daemon synthesized a flat `~/.gobby/session_wiki/{external_id}.md` for
//! a session, this module generates the *same* handoff summary directly from a
//! raw transcript archive, then wraps it in the daemon `.md` format so it flows
//! through the identical `ingest_session_wiki_file_without_index` path as a real
//! daemon file.
//!
//! The prompt is vendored from the daemon at `prompts/handoff_session_end.md`
//! (source of truth:
//! `gobby/src/gobby/install/shared/prompts/handoff/session_end.md`, locked by the
//! daemon golden test `tests/prompts/test_handoff_session_end_golden.py`). The
//! body is constrained to the pure `{{ variable }}` substitution subset, so a
//! strict-undefined `minijinja` render matches the daemon's Jinja2 render for
//! plain-string values.
//!
//! Standalone runs have no hub, so prompt variables that require hub context
//! (tasks, memories, git) are filled with explicit sentinels; the page is marked
//! `summary_mode: standalone` and is always superseded by a later daemon
//! synthesis (latest-wins, keyed on `session:{external_id}`).

use std::path::Path;

/// Resolved, per-batch session summarizer.
///
/// Construct once with [`SessionSummarizer::resolve`]. `None` means standalone
/// summarization is unavailable for this batch (flag off, AI routed off or
/// unresolvable, or compiled without the `ai` feature) and the caller falls back
/// to the raw skeleton page.
pub(crate) struct SessionSummarizer {
    #[cfg(feature = "ai")]
    context: gobby_core::ai_context::AiContext,
    #[cfg(feature = "ai")]
    route: gobby_core::config::AiRouting,
}

impl SessionSummarizer {
    /// Resolve AI config once for the whole sync batch. Returns `None` when
    /// `summarize` is off, the AI config can't be resolved, or text generation is
    /// routed off — the caller then degrades to skeleton pages.
    #[cfg(feature = "ai")]
    pub(crate) fn resolve(summarize: bool) -> Option<Self> {
        use gobby_core::ai::effective_route;
        use gobby_core::ai_context::{AiContext, AiContextOptions};
        use gobby_core::config::{AiCapability, AiRouting};

        if !summarize {
            return None;
        }
        let mut source = match crate::support::config::hub_ai_config_source("gwiki sync-sessions") {
            Ok(source) => source,
            Err(error) => {
                log::warn!("sync-sessions --summarize: could not resolve AI config: {error}");
                return None;
            }
        };
        let context = AiContext::resolve_with_options(
            None,
            &mut source,
            AiContextOptions {
                no_ai: false,
                forced_routing: None,
            },
        );
        let route = effective_route(&context, AiCapability::TextGenerate);
        if matches!(route, AiRouting::Direct | AiRouting::Daemon) {
            Some(Self { context, route })
        } else {
            log::warn!(
                "sync-sessions --summarize: text generation is routed off; writing skeleton pages"
            );
            None
        }
    }

    #[cfg(not(feature = "ai"))]
    pub(crate) fn resolve(_summarize: bool) -> Option<Self> {
        None
    }

    /// Generate the daemon-format `.md` bytes for one raw archive, or `None` to
    /// fall back to the skeleton page (parse failure, ineligible session, empty
    /// or failed generation). Never panics; degradation is always graceful.
    #[cfg(feature = "ai")]
    pub(crate) fn summarize_archive(
        &self,
        path: &Path,
        bytes: &[u8],
        external_id: &str,
    ) -> Option<Vec<u8>> {
        use gobby_core::ai::{daemon, text};
        use gobby_core::config::AiRouting;

        let parsed = match super::parse_session_archive_bytes(path, bytes) {
            Ok(parsed) => parsed,
            // The skeleton fallback re-parses and reports the real error.
            Err(_) => return None,
        };
        if parsed.metadata.is_subagent {
            return None;
        }
        if is_ephemeral_session_type(&parsed.session_type) {
            return None;
        }

        let context = build_standalone_summary_context(&parsed, external_id);
        let prompt = match render_summary_prompt(&context) {
            Ok(prompt) => prompt,
            Err(error) => {
                log::warn!(
                    "sync-sessions --summarize: prompt render failed for {external_id}: {error}"
                );
                return None;
            }
        };
        let result = match self.route {
            AiRouting::Direct => {
                text::generate_text(&self.context, &prompt, Some(summary_system()))
            }
            AiRouting::Daemon => {
                daemon::generate_via_daemon(&self.context, &prompt, Some(summary_system()))
            }
            // resolve() only stores Direct/Daemon.
            _ => return None,
        };
        match result {
            Ok(result) if !result.text.trim().is_empty() => Some(assemble_standalone_wiki_md(
                &parsed,
                external_id,
                &result.text,
            )),
            Ok(_) => {
                log::warn!(
                    "sync-sessions --summarize: empty summary for {external_id}; writing skeleton"
                );
                None
            }
            Err(error) => {
                log::warn!(
                    "sync-sessions --summarize: generation failed for {external_id}: {error}"
                );
                None
            }
        }
    }

    #[cfg(not(feature = "ai"))]
    pub(crate) fn summarize_archive(
        &self,
        _path: &Path,
        _bytes: &[u8],
        _external_id: &str,
    ) -> Option<Vec<u8>> {
        None
    }
}

#[cfg(feature = "ai")]
use super::{ParsedSession, ParsedSessionMessage};

/// Vendored copy of the daemon's `handoff/session_end` prompt (frontmatter +
/// body). See the module docs for the source of truth and drift lock.
#[cfg(feature = "ai")]
const PROMPT_TEMPLATE: &str = include_str!("prompts/handoff_session_end.md");

/// Filled in for every prompt variable that needs hub context the standalone
/// path cannot supply. Explicit (not blank) so a strict render succeeds and the
/// model is not misled into asserting "no changes".
#[cfg(feature = "ai")]
const STANDALONE_SENTINEL: &str =
    "(not available - generated standalone from the transcript, without daemon/hub context)";

/// Char budget for the larger "Recent Turns" transcript slice.
#[cfg(feature = "ai")]
const TRANSCRIPT_TAIL_BUDGET: usize = 16_000;
/// Char budget for the smaller "Last Messages" slice.
#[cfg(feature = "ai")]
const LAST_MESSAGES_BUDGET: usize = 4_000;
/// Per-message cap so a single huge message can't dominate the prompt.
#[cfg(feature = "ai")]
const PER_MESSAGE_CHAR_CAP: usize = 4_000;

#[cfg(feature = "ai")]
fn summary_system() -> &'static str {
    "You are a precise engineering session summarizer. Use only the provided \
     session data. Output only the requested markdown summary sections; do not \
     add a preamble, restate these instructions, or invent facts that are not \
     present in the transcript."
}

/// Sessions whose `session_type` marks them as ephemeral are not worth an LLM
/// summary (mirrors the daemon's pipeline/cron skip).
#[cfg(feature = "ai")]
fn is_ephemeral_session_type(session_type: &str) -> bool {
    matches!(
        session_type.trim().to_ascii_lowercase().as_str(),
        "pipeline" | "cron"
    )
}

/// Strip a leading `---`-delimited frontmatter block and trim, mirroring the
/// daemon's `parse_frontmatter(raw)[1].strip()` (what `sync_bundled_prompts`
/// seeds into the hub and renders).
#[cfg(feature = "ai")]
fn prompt_body(raw: &str) -> String {
    let raw = raw.strip_prefix('\u{feff}').unwrap_or(raw);
    let mut lines = raw.lines();
    if lines.next().map(str::trim_end) != Some("---") {
        return raw.trim().to_string();
    }
    let mut body = Vec::new();
    let mut closed = false;
    for line in lines {
        if !closed {
            if line.trim_end() == "---" {
                closed = true;
            }
            continue;
        }
        body.push(line);
    }
    if !closed {
        return raw.trim().to_string();
    }
    body.join("\n").trim().to_string()
}

/// Render the prompt body from `context` with minijinja under strict-undefined
/// semantics. The shared prompt is constrained to the pure `{{ variable }}`
/// subset, so this matches the daemon's Jinja2 render for plain-string values.
/// Strict-undefined makes a future prompt change that adds an unsupplied variable
/// fail loudly (the caller then degrades to the skeleton) rather than silently
/// emitting a blank.
#[cfg(feature = "ai")]
fn render_summary_prompt(
    context: &std::collections::BTreeMap<&'static str, String>,
) -> Result<String, crate::WikiError> {
    use minijinja::{Environment, UndefinedBehavior};

    let mut env = Environment::new();
    env.set_undefined_behavior(UndefinedBehavior::Strict);
    let body = prompt_body(PROMPT_TEMPLATE);
    env.render_str(&body, context)
        .map_err(|error| crate::WikiError::Config {
            detail: format!("failed to render session summary prompt: {error}"),
        })
}

/// Build the reduced, transcript-only prompt context. Every prompt variable is
/// supplied: the transcript slices carry the real signal; hub-only fields get
/// explicit [`STANDALONE_SENTINEL`] values.
#[cfg(feature = "ai")]
fn build_standalone_summary_context(
    parsed: &ParsedSession,
    external_id: &str,
) -> std::collections::BTreeMap<&'static str, String> {
    use std::collections::BTreeMap;

    let mut context = BTreeMap::new();
    // Real signal from the transcript.
    context.insert(
        "transcript_summary",
        render_message_tail(&parsed.messages, TRANSCRIPT_TAIL_BUDGET),
    );
    context.insert(
        "last_messages",
        render_message_tail(&parsed.messages, LAST_MESSAGES_BUDGET),
    );
    // Minimal real origin so the model has grounding without a hub.
    let source = non_empty(&parsed.session_type).unwrap_or("unknown");
    context.insert(
        "structured_context",
        format!(
            "Standalone summary generated without daemon/hub context. \
             session_id: {external_id}; source: {source}; messages: {}.",
            parsed.messages.len()
        ),
    );
    // Hub-only fields: explicit sentinels (see STANDALONE_SENTINEL).
    for key in [
        "git_status",
        "file_changes",
        "git_diff_summary",
        "claimed_tasks",
        "session_memories",
        "first_digest_turn",
        "recent_digest_turns",
    ] {
        context.insert(key, STANDALONE_SENTINEL.to_string());
    }
    context
}

/// Render the most-recent messages that fit within `char_budget`, in
/// chronological order, each capped at [`PER_MESSAGE_CHAR_CAP`].
#[cfg(feature = "ai")]
fn render_message_tail(messages: &[ParsedSessionMessage], char_budget: usize) -> String {
    if messages.is_empty() {
        return "(transcript contained no messages)".to_string();
    }
    let mut blocks: Vec<String> = Vec::new();
    let mut used = 0usize;
    for message in messages.iter().rev() {
        let heading = super::message_heading(&message.role);
        let content = truncate_chars(message.content.trim(), PER_MESSAGE_CHAR_CAP);
        let body = if content.is_empty() {
            "(no text content)".to_string()
        } else {
            content
        };
        let block = format!("### {heading}\n{body}");
        used = used.saturating_add(block.chars().count() + 2);
        blocks.push(block);
        if used >= char_budget {
            break;
        }
    }
    blocks.reverse();
    blocks.join("\n\n")
}

/// Wrap an LLM summary `body` in the daemon `.md` format so it parses through
/// `DaemonWikiPage::parse` and renders via `render_session_wiki_markdown` like a
/// real daemon file. `summary_mode: standalone` distinguishes it in the vault.
#[cfg(feature = "ai")]
fn assemble_standalone_wiki_md(parsed: &ParsedSession, external_id: &str, body: &str) -> Vec<u8> {
    use crate::ingest::single_line;

    let title = non_empty(&parsed.title)
        .map(single_line)
        .unwrap_or_else(|| format!("Session {external_id}"));
    let source = non_empty(&parsed.session_type)
        .map(single_line)
        .unwrap_or_default();
    let model = parsed
        .metadata
        .model
        .as_deref()
        .map(single_line)
        .unwrap_or_default();

    let mut doc = String::new();
    doc.push_str("---\n");
    doc.push_str(&format!("title: {title}\n"));
    doc.push_str(&format!("source: {source}\n"));
    doc.push_str(&format!("model: {model}\n"));
    doc.push_str("tags: []\n");
    doc.push_str(&format!("session_id: {}\n", single_line(external_id)));
    doc.push_str("summary_mode: standalone\n");
    doc.push_str("---\n");
    doc.push_str(body.trim());
    doc.push('\n');
    doc.into_bytes()
}

#[cfg(feature = "ai")]
fn truncate_chars(value: &str, max_chars: usize) -> String {
    if value.chars().count() <= max_chars {
        value.to_string()
    } else {
        value.chars().take(max_chars).collect()
    }
}

#[cfg(feature = "ai")]
fn non_empty(value: &str) -> Option<&str> {
    let trimmed = value.trim();
    (!trimmed.is_empty()).then_some(trimmed)
}

#[cfg(all(test, feature = "ai"))]
mod tests {
    use super::super::ParsedSessionMetadata;
    use super::*;

    const REQUIRED_VARIABLES: &[&str] = &[
        "transcript_summary",
        "last_messages",
        "git_status",
        "file_changes",
    ];
    const OPTIONAL_VARIABLES: &[&str] = &[
        "structured_context",
        "git_diff_summary",
        "claimed_tasks",
        "session_memories",
        "first_digest_turn",
        "recent_digest_turns",
    ];

    fn message(role: &str, content: &str) -> ParsedSessionMessage {
        ParsedSessionMessage {
            role: role.to_string(),
            timestamp: None,
            content: content.to_string(),
            tool_names: Vec::new(),
        }
    }

    #[test]
    fn resolve_is_none_when_summarize_disabled() {
        assert!(
            SessionSummarizer::resolve(false).is_none(),
            "summarization must stay off until explicitly requested"
        );
    }

    fn parsed(messages: Vec<ParsedSessionMessage>) -> ParsedSession {
        ParsedSession {
            title: "Refactor the indexer".to_string(),
            session_type: "claude".to_string(),
            started_at: None,
            metadata: ParsedSessionMetadata {
                model: Some("claude-opus-4-8".to_string()),
                ..ParsedSessionMetadata::default()
            },
            messages,
        }
    }

    #[test]
    fn prompt_body_strips_frontmatter() {
        let body = prompt_body(PROMPT_TEMPLATE);
        assert!(!body.starts_with("---"), "frontmatter must be stripped");
        assert!(!body.contains("required_variables:"));
        assert!(body.starts_with("You are generating a session summary"));
        assert!(body.contains("{{ transcript_summary }}"));
    }

    #[test]
    fn render_substitutes_every_variable() {
        let context = build_standalone_summary_context(
            &parsed(vec![
                message("user", "hello"),
                message("assistant", "world"),
            ]),
            "sess-1",
        );
        let rendered = render_summary_prompt(&context).expect("render");
        assert!(
            !rendered.contains("{{"),
            "no unsubstituted placeholders should remain: {rendered}"
        );
        // Both transcript messages and a hub-field sentinel are interpolated.
        assert!(rendered.contains("hello"));
        assert!(rendered.contains("world"));
        assert!(rendered.contains(STANDALONE_SENTINEL));
    }

    #[test]
    fn render_is_strict_on_unknown_variables() {
        let mut context = std::collections::BTreeMap::new();
        // Supply only some variables; the body references more.
        context.insert("transcript_summary", "x".to_string());
        let error = render_summary_prompt(&context).expect_err("must reject missing variables");
        assert!(matches!(error, crate::WikiError::Config { .. }));
    }

    #[test]
    fn context_supplies_full_variable_contract() {
        let context = build_standalone_summary_context(
            &parsed(vec![message("user", "only message")]),
            "sess-2",
        );
        for name in REQUIRED_VARIABLES.iter().chain(OPTIONAL_VARIABLES) {
            assert!(
                context.contains_key(name),
                "missing prompt variable `{name}`"
            );
        }
        // Hub-only fields are sentinels; the transcript field is real.
        assert_eq!(context["git_status"], STANDALONE_SENTINEL);
        assert!(context["last_messages"].contains("only message"));
    }

    #[test]
    fn message_tail_keeps_most_recent_within_budget() {
        let messages = vec![
            message("user", "first turn"),
            message("assistant", "middle turn"),
            message("user", "final turn"),
        ];
        let rendered = render_message_tail(&messages, LAST_MESSAGES_BUDGET);
        assert!(rendered.contains("final turn"));
        // Chronological order is preserved.
        let first = rendered.find("first turn");
        let last = rendered.find("final turn");
        if let (Some(first), Some(last)) = (first, last) {
            assert!(first < last, "messages must render in chronological order");
        }
    }

    #[test]
    fn assemble_emits_standalone_provenance_frontmatter() {
        let bytes = assemble_standalone_wiki_md(
            &parsed(vec![message("user", "hi")]),
            "sess-3",
            "## Current State\nAll green.\n",
        );
        let doc = String::from_utf8(bytes).expect("utf8");
        assert!(doc.starts_with("---\n"));
        assert!(doc.contains("title: Refactor the indexer\n"));
        assert!(doc.contains("source: claude\n"));
        assert!(doc.contains("model: claude-opus-4-8\n"));
        assert!(doc.contains("summary_mode: standalone\n"));
        assert!(doc.contains("## Current State"));
        assert!(doc.trim_end().ends_with("All green."));
    }
}
