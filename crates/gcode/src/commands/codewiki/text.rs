use std::fmt::Write as _;
use std::time::Duration;

use gobby_core::ai_types::AiError;

use super::*;

const FALLBACK_CITATION_LINE_WIDTH: usize = 240;

/// Backoff between generation attempts; the array length bounds the retries.
const GENERATION_RETRY_BACKOFF: [Duration; 2] =
    [Duration::from_millis(200), Duration::from_millis(500)];

#[derive(serde::Serialize)]
struct Frontmatter<'a> {
    title: &'a str,
    #[serde(rename = "type")]
    kind: &'a str,
    provenance: Vec<FrontmatterSourceFile<'a>>,
    generated_by: &'static str,
    trust: &'static str,
    freshness: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    degraded: Option<bool>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    degraded_sources: Vec<&'a str>,
}

#[derive(Clone, serde::Serialize)]
struct FrontmatterSourceFile<'a> {
    file: &'a str,
    ranges: Vec<String>,
}

pub(crate) fn resolve_text_generator(
    ctx: &Context,
    ai: &CodewikiAiOptions,
) -> Option<Box<TextGenerator<'static>>> {
    let ai_context = resolve_ai_context(ctx, ai.routing).ok()?;
    let route = effective_route(&ai_context, AiCapability::TextGenerate);
    if matches!(route, AiRouting::Off | AiRouting::Auto) {
        return None;
    }

    let aggregate_profile = ai
        .aggregate_profile
        .clone()
        .unwrap_or_else(|| DEFAULT_AGGREGATE_PROFILE.to_string());
    let mut warned = false;
    let quiet = ctx.quiet;
    Some(Box::new(move |prompt, system, tier| {
        let profile = match tier {
            PromptTier::Aggregate => Some(aggregate_profile.as_str()),
            PromptTier::Standard => None,
        };
        let result = generate_with_bounded_retry(|| match route {
            AiRouting::Daemon => generate_via_daemon_with_max_tokens(
                &ai_context,
                prompt,
                Some(system),
                None,
                profile,
            ),
            AiRouting::Direct => generate_text(&ai_context, prompt, Some(system)),
            AiRouting::Off | AiRouting::Auto => {
                unreachable!("non-generating routes returned above")
            }
        });
        match result {
            Ok(result) => clean_generated(result.text),
            Err(error) => {
                if !quiet && !warned {
                    eprintln!(
                        "text generation failed; affected codewiki docs fall back to AST-only \
                         content and record degraded: true: {error}"
                    );
                    warned = true;
                }
                None
            }
        }
    }))
}

/// Retries transient generation failures with a short backoff so one dropped
/// connection does not cost a doc its prose for the whole run. Non-transient
/// errors (bad config, parse failures, 4xx) fail immediately.
pub(crate) fn generate_with_bounded_retry<T>(
    mut call: impl FnMut() -> Result<T, AiError>,
) -> Result<T, AiError> {
    let mut result = call();
    for backoff in GENERATION_RETRY_BACKOFF {
        match &result {
            Err(error) if retryable_generation_error(error) => {
                std::thread::sleep(backoff);
                result = call();
            }
            _ => break,
        }
    }
    result
}

fn retryable_generation_error(error: &AiError) -> bool {
    match error {
        AiError::TransportFailure { .. } | AiError::RateLimited { .. } => true,
        AiError::HttpStatus { status, .. } => *status >= 500,
        AiError::CapabilityUnavailable { .. }
        | AiError::NotConfigured { .. }
        | AiError::ParseFailure { .. } => false,
    }
}

pub(crate) fn resolve_ai_context(
    ctx: &Context,
    ai: Option<AiRouting>,
) -> anyhow::Result<AiContext> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let standalone = config::read_standalone_config_optional();
    let primary = PostgresAiConfigSource::new(&mut conn, secrets::resolve_config_value);
    let mut source = AiConfigSource::with_primary(primary, standalone);
    Ok(AiContext::resolve_with_options(
        Some(ctx.project_id.clone()),
        &mut source,
        AiContextOptions {
            no_ai: false,
            forced_routing: ai,
        },
    ))
}

/// Outcome of one optional generation attempt. `Failed` means a generator ran
/// and produced no usable text — the doc is degraded relative to what the run
/// intended. `Skipped` means no generator ran (AI off, or gated by depth), so
/// structural output is the intent, not a degradation.
#[derive(Debug)]
pub(crate) enum Generation {
    Generated(String),
    Failed,
    Skipped,
}

impl Generation {
    pub(crate) fn failed(&self) -> bool {
        matches!(self, Generation::Failed)
    }

    /// Returns generated text, or `fallback` while flagging `degraded` when
    /// the generator attempted and failed.
    pub(crate) fn unwrap_or_record(self, fallback: String, degraded: &mut bool) -> String {
        match self {
            Generation::Generated(text) => text,
            Generation::Failed => {
                *degraded = true;
                fallback
            }
            Generation::Skipped => fallback,
        }
    }
}

pub(crate) fn maybe_generate(
    generate: &mut Option<&mut TextGenerator<'_>>,
    prompt: &str,
    system: &str,
    tier: PromptTier,
) -> Generation {
    match generate.as_deref_mut() {
        None => Generation::Skipped,
        Some(generate) => match generate(prompt, system, tier) {
            Some(text) if is_prompt_echo(&text, prompt) => Generation::Failed,
            Some(text) => Generation::Generated(text),
            None => Generation::Failed,
        },
    }
}

/// Echo detection floor: prompts shorter than this never trigger rejection,
/// and only this much of the prompt head has to reappear to count as an echo.
const PROMPT_ECHO_PREFIX_CHARS: usize = 80;

/// True when the generated text starts by repeating the prompt itself — a
/// failure mode of overloaded models on huge prompts that previously poisoned
/// pages and recorded summaries as healthy output (#698).
pub(crate) fn is_prompt_echo(text: &str, prompt: &str) -> bool {
    let prefix = prompt
        .trim_start()
        .chars()
        .take(PROMPT_ECHO_PREFIX_CHARS)
        .collect::<String>();
    if prefix.chars().count() < PROMPT_ECHO_PREFIX_CHARS {
        return false;
    }
    text.trim_start().starts_with(&prefix)
}

pub(crate) fn clean_generated(text: String) -> Option<String> {
    let text = text.trim();
    (!text.is_empty()).then(|| text.to_string())
}

pub(crate) fn structural_symbol_purpose(symbol: &Symbol) -> String {
    if let Some(summary) = symbol.summary.as_deref().filter(|value| !value.is_empty()) {
        return summary.to_string();
    }
    if let Some(docstring) = symbol
        .docstring
        .as_deref()
        .filter(|value| !value.is_empty())
    {
        return docstring.to_string();
    }
    format!(
        "Indexed {} `{}` in `{}`.",
        symbol.kind, symbol.qualified_name, symbol.file_path
    )
}

pub(crate) fn structural_file_summary(file: &str, symbols: &[SymbolDoc]) -> String {
    if symbols.is_empty() {
        return format!("`{file}` has no indexed API symbols.");
    }
    format!(
        "`{file}` exposes {} indexed API symbol{}.",
        symbols.len(),
        plural(symbols.len())
    )
}

pub(crate) fn structural_module_summary(
    module: &str,
    files: &[FileLink],
    child_modules: &[ModuleLink],
) -> String {
    let file_count = files.len();
    let child_count = child_modules.len();
    format!(
        "`{module}` contains {file_count} direct file{} and {child_count} child module{}.",
        plural(file_count),
        plural(child_count)
    )
}

pub(crate) fn structural_repo_summary(file_count: usize, module_count: usize) -> String {
    format!(
        "Repository code documentation covers {file_count} file{} across {module_count} module{}.",
        plural(file_count),
        plural(module_count)
    )
}

pub(crate) fn write_section(doc: &mut String, heading: &str, body: &str) {
    let _ = writeln!(doc, "## {heading}\n\n{}\n", body.trim());
}

pub(crate) fn collect_link_spans(files: &[FileLink], modules: &[ModuleLink]) -> Vec<SourceSpan> {
    let mut spans = BTreeSet::new();
    for file in files {
        spans.extend(file.source_spans.iter().cloned());
    }
    for module in modules {
        spans.extend(module.source_spans.iter().cloned());
    }
    spans.into_iter().collect()
}

/// Hard cap on fallback citations appended when generated prose carries no
/// valid inline citation. Aggregate pages can cover thousands of spans;
/// appending the full set produced megabyte citation walls that re-entered
/// downstream summaries and prompts (#699).
pub(crate) const MAX_FALLBACK_CITATIONS: usize = 5;

/// Representative subset of `spans` for fallback citations: at most
/// [`MAX_FALLBACK_CITATIONS`] entries, preferring one span per distinct file
/// so broad pages cite breadth rather than one file's span run.
fn fallback_spans(spans: &[SourceSpan]) -> Vec<SourceSpan> {
    let deduped = spans.iter().cloned().collect::<BTreeSet<_>>();
    let mut picked = Vec::new();
    let mut seen_files = BTreeSet::new();
    for span in &deduped {
        if picked.len() == MAX_FALLBACK_CITATIONS {
            return picked;
        }
        if seen_files.insert(span.file.clone()) {
            picked.push(span.clone());
        }
    }
    for span in deduped {
        if picked.len() == MAX_FALLBACK_CITATIONS {
            break;
        }
        if !picked.contains(&span) {
            picked.push(span);
        }
    }
    picked
}

pub(crate) fn citation_list(spans: &[SourceSpan]) -> String {
    fallback_spans(spans)
        .into_iter()
        .map(|span| span.citation())
        .collect::<Vec<_>>()
        .join("\n")
}

fn wrap_citation_items<I>(items: I, max_line_width: usize) -> String
where
    I: IntoIterator<Item = String>,
{
    let mut lines = Vec::new();
    let mut line = String::new();
    for item in items {
        let separator_width = usize::from(!line.is_empty());
        if !line.is_empty() && line.len() + separator_width + item.len() > max_line_width {
            lines.push(std::mem::take(&mut line));
        }
        if !line.is_empty() {
            line.push(' ');
        }
        line.push_str(&item);
    }
    if !line.is_empty() {
        lines.push(line);
    }
    lines.join("\n")
}

pub(crate) fn citation_markers(spans: &[SourceSpan]) -> String {
    let fallback = fallback_spans(spans)
        .into_iter()
        .map(|span| span.citation())
        .collect::<BTreeSet<_>>();
    wrap_citation_items(
        citation_references(spans)
            .into_iter()
            .filter(|(_, citation)| fallback.contains(citation))
            .map(|(index, _)| format!("[{index}]")),
        FALLBACK_CITATION_LINE_WIDTH,
    )
}

fn citation_references(spans: &[SourceSpan]) -> Vec<(usize, String)> {
    spans
        .iter()
        .cloned()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .enumerate()
        .map(|(index, span)| (index + 1, span.citation()))
        .collect()
}

pub(crate) fn replace_citations_with_markers(text: &str, spans: &[SourceSpan]) -> String {
    let mut marked = text.to_string();
    for (index, citation) in citation_references(spans) {
        marked = marked.replace(&citation, &format!("[{index}]"));
    }
    marked
}

/// Appends a References section resolving only the `[N]` markers that appear
/// in `doc`; unreferenced spans stay out so the section scales with the prose
/// rather than with everything the page covers (#699).
pub(crate) fn write_references(doc: &mut String, spans: &[SourceSpan]) {
    let references = citation_references(spans)
        .into_iter()
        .filter(|(index, _)| doc.contains(&format!("[{index}]")))
        .collect::<Vec<_>>();
    if references.is_empty() {
        return;
    }
    doc.push_str("## References\n\n");
    for (index, citation) in references {
        let _ = writeln!(doc, "- [{index}] {citation}");
    }
    doc.push('\n');
}

pub(crate) fn ground_text(
    text: &str,
    valid_spans: &[SourceSpan],
    fallback_citation: Option<&str>,
) -> String {
    let cleaned = strip_invalid_citations(text, valid_spans);
    match fallback_citation {
        Some(fallback_citation) if !contains_valid_citation(&cleaned, valid_spans) => {
            if fallback_citation.contains('\n') {
                format!("{cleaned}\n{fallback_citation}")
            } else {
                format!("{cleaned} {fallback_citation}")
            }
        }
        _ => cleaned,
    }
}

pub(crate) fn strip_invalid_citations(text: &str, valid_spans: &[SourceSpan]) -> String {
    let mut out = String::new();
    let mut rest = text;
    while let Some(open) = rest.find('[') {
        let (before, after_open) = rest.split_at(open);
        out.push_str(before);
        let after_open = &after_open[1..];
        let Some(close) = after_open.find(']') else {
            out.push('[');
            out.push_str(after_open);
            return out;
        };
        let candidate = &after_open[..close];
        if citation_parts(candidate).is_none_or(|(file, start, end)| {
            valid_spans
                .iter()
                .any(|span| span.contains(file, start, end))
        }) {
            out.push('[');
            out.push_str(candidate);
            out.push(']');
        }
        rest = &after_open[close + 1..];
    }
    out.push_str(rest);
    out
}

pub(crate) fn contains_valid_citation(text: &str, valid_spans: &[SourceSpan]) -> bool {
    let mut rest = text;
    while let Some(open) = rest.find('[') {
        let after_open = &rest[open + 1..];
        let Some(close) = after_open.find(']') else {
            return false;
        };
        if let Some((file, start, end)) = citation_parts(&after_open[..close])
            && valid_spans
                .iter()
                .any(|span| span.contains(file, start, end))
        {
            return true;
        }
        rest = &after_open[close + 1..];
    }
    false
}

pub(crate) fn citation_parts(value: &str) -> Option<(&str, usize, usize)> {
    let (file, range) = value.rsplit_once(':')?;
    if file.is_empty() || file.chars().any(char::is_whitespace) {
        return None;
    }
    let (line_start, line_end) = match range.split_once('-') {
        Some((start, end)) => (start.parse().ok()?, end.parse().ok()?),
        None => {
            let line = range.parse().ok()?;
            (line, line)
        }
    };
    (line_start > 0 && line_start <= line_end).then_some((file, line_start, line_end))
}

#[cfg(test)]
pub(crate) fn frontmatter(title: &str, kind: &str, source_spans: &[SourceSpan]) -> String {
    frontmatter_with_degradation(title, kind, source_spans, &[])
}

/// Builds the same generated frontmatter as [`frontmatter`], plus optional
/// `degraded` and `degraded_sources` fields when graph/AI inputs are partial.
pub(crate) fn frontmatter_with_degradation(
    title: &str,
    kind: &str,
    source_spans: &[SourceSpan],
    degraded_sources: &[String],
) -> String {
    let mut files: BTreeMap<&str, BTreeSet<(usize, usize)>> = BTreeMap::new();
    for span in source_spans {
        files
            .entry(&span.file)
            .or_default()
            .insert((span.line_start, span.line_end));
    }

    let source_files: Vec<FrontmatterSourceFile<'_>> = files
        .into_iter()
        .map(|(file, ranges)| FrontmatterSourceFile {
            file,
            ranges: ranges
                .into_iter()
                .map(|(line_start, line_end)| {
                    if line_start == line_end {
                        line_start.to_string()
                    } else {
                        format!("{line_start}-{line_end}")
                    }
                })
                .collect(),
        })
        .collect();
    let data = Frontmatter {
        title,
        kind,
        provenance: source_files,
        generated_by: "gcode-codewiki",
        trust: "generated",
        freshness: "indexed",
        degraded: (!degraded_sources.is_empty()).then_some(true),
        degraded_sources: degraded_sources.iter().map(String::as_str).collect(),
    };
    let yaml = serde_yaml::to_string(&data)
        .expect("codewiki frontmatter only contains YAML-serializable data");
    let yaml = yaml.strip_prefix("---\n").unwrap_or(&yaml);

    let mut out = String::from("---\n");
    out.push_str(yaml);
    if !out.ends_with('\n') {
        out.push('\n');
    }
    out.push_str("---\n\n");
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn span(file: impl Into<String>, line_start: usize, line_end: usize) -> SourceSpan {
        SourceSpan {
            file: file.into(),
            line_start,
            line_end,
        }
    }

    #[test]
    fn citation_list_emits_one_fallback_range_per_line() {
        let spans = (0..3)
            .map(|index| {
                span(
                    format!(
                        "crates/gcode/src/generated/deep/module/path/with/long/components/file_{index}.rs",
                    ),
                    index + 1,
                    index + 10,
                )
            })
            .collect::<Vec<_>>();

        let citations = citation_list(&spans);

        let lines = citations.lines().collect::<Vec<_>>();
        assert_eq!(lines.len(), spans.len(), "{citations}");
        for (line, span) in lines.iter().zip(spans) {
            assert_eq!(*line, span.citation());
        }
    }

    #[test]
    fn citation_list_caps_oversized_span_sets() {
        let spans = (0..200)
            .map(|index| span(format!("src/file_{index:03}.rs"), 1, 10))
            .collect::<Vec<_>>();

        let citations = citation_list(&spans);

        assert_eq!(
            citations.lines().count(),
            MAX_FALLBACK_CITATIONS,
            "{citations}"
        );
    }

    #[test]
    fn fallback_spans_prefer_distinct_files_over_one_files_span_run() {
        let mut spans = (1..100)
            .map(|line| span("src/big.rs", line, line))
            .collect::<Vec<_>>();
        spans.push(span("src/other.rs", 1, 5));

        let picked = fallback_spans(&spans);

        assert!(picked.len() <= MAX_FALLBACK_CITATIONS);
        assert!(
            picked.iter().any(|span| span.file == "src/other.rs"),
            "distinct file must be represented: {picked:?}"
        );
    }

    #[test]
    fn citation_markers_are_capped_and_keep_reference_numbering() {
        let spans = (0..80)
            .map(|index| span(format!("src/file_{index:02}.rs"), 1, 1))
            .collect::<Vec<_>>();

        let markers = citation_markers(&spans);

        assert_eq!(
            markers.split_whitespace().count(),
            MAX_FALLBACK_CITATIONS,
            "{markers}"
        );
        assert!(markers.starts_with("[1]"), "{markers}");
    }

    #[test]
    fn references_resolve_only_markers_present_in_doc() {
        let spans = (0..40)
            .map(|index| span(format!("src/file_{index:02}.rs"), 1, 1))
            .collect::<Vec<_>>();
        let mut doc = "Prose citing [2] and [17] only.\n\n".to_string();

        write_references(&mut doc, &spans);

        let references = doc
            .lines()
            .filter(|line| line.starts_with("- ["))
            .collect::<Vec<_>>();
        assert_eq!(references.len(), 2, "{doc}");
        assert!(references[0].starts_with("- [2] "), "{doc}");
        assert!(references[1].starts_with("- [17] "), "{doc}");
    }

    #[test]
    fn wrap_citation_items_bounds_line_width() {
        let items = (0..80).map(|index| format!("[{index}]"));

        let wrapped = wrap_citation_items(items, 40);

        assert!(wrapped.lines().count() > 1, "{wrapped}");
        assert!(wrapped.lines().all(|line| line.len() <= 40), "{wrapped}");
    }

    #[test]
    fn prompt_echo_is_rejected_as_failed_generation() {
        let prompt = prompts::module_prompt(
            "crates/gcode",
            &[prompts::ChildSummary {
                name: "crates/gcode/Cargo.toml".to_string(),
                summary: "Manifest for the gcode binary.".to_string(),
            }],
            &[],
            &[],
        );

        let mut echoing = |prompt: &str, _system: &str, _tier: PromptTier| Some(prompt.to_string());
        let mut generate = Some::<&mut TextGenerator<'_>>(&mut echoing);
        let generation = maybe_generate(
            &mut generate,
            &prompt,
            prompts::MODULE_SYSTEM,
            PromptTier::Aggregate,
        );
        assert!(generation.failed(), "prompt echo must record degradation");

        let mut healthy = |_prompt: &str, _system: &str, _tier: PromptTier| {
            Some("`crates/gcode` indexes source and serves search.".to_string())
        };
        let mut generate = Some::<&mut TextGenerator<'_>>(&mut healthy);
        let generation = maybe_generate(
            &mut generate,
            &prompt,
            prompts::MODULE_SYSTEM,
            PromptTier::Aggregate,
        );
        assert!(matches!(generation, Generation::Generated(_)));
    }

    #[test]
    fn short_prompts_never_trigger_echo_rejection() {
        let prompt = "Short prompt.";
        assert!(!is_prompt_echo("Short prompt.", prompt));
    }

    fn transport_failure() -> AiError {
        AiError::TransportFailure {
            status: None,
            body: None,
            source: "connection reset".to_string(),
        }
    }

    #[test]
    fn bounded_retry_recovers_from_transient_transport_failure() {
        let mut calls = 0_usize;
        let result = generate_with_bounded_retry(|| {
            calls += 1;
            if calls == 1 {
                Err(transport_failure())
            } else {
                Ok("generated".to_string())
            }
        });

        assert_eq!(result.expect("retry recovers"), "generated");
        assert_eq!(calls, 2);
    }

    #[test]
    fn bounded_retry_gives_up_after_bounded_attempts() {
        let mut calls = 0_usize;
        let result: Result<String, AiError> = generate_with_bounded_retry(|| {
            calls += 1;
            Err(transport_failure())
        });

        assert!(result.is_err());
        assert_eq!(calls, 1 + GENERATION_RETRY_BACKOFF.len());
    }

    #[test]
    fn bounded_retry_fails_fast_on_non_transient_errors() {
        let mut calls = 0_usize;
        let result: Result<String, AiError> = generate_with_bounded_retry(|| {
            calls += 1;
            Err(AiError::NotConfigured {
                capability: None,
                message: "no provider".to_string(),
            })
        });

        assert!(result.is_err());
        assert_eq!(calls, 1);
    }
}
