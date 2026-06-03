use std::fmt::Write as _;

use super::*;

pub(crate) fn resolve_text_generator(
    ctx: &Context,
    ai: Option<AiRouting>,
) -> Option<Box<TextGenerator<'static>>> {
    let ai_context = resolve_ai_context(ctx, ai).ok()?;
    let route = effective_route(&ai_context, AiCapability::TextGenerate);
    if matches!(route, AiRouting::Off | AiRouting::Auto) {
        return None;
    }

    let mut warned = false;
    let quiet = ctx.quiet;
    Some(Box::new(move |prompt, system| {
        let result = match route {
            AiRouting::Daemon => generate_via_daemon(&ai_context, prompt, Some(system)),
            AiRouting::Direct => generate_text(&ai_context, prompt, Some(system)),
            AiRouting::Off | AiRouting::Auto => return None,
        };
        match result {
            Ok(result) => clean_generated(result.text),
            Err(error) => {
                if !quiet && !warned {
                    eprintln!("text generation unavailable; using AST-only codewiki docs: {error}");
                    warned = true;
                }
                None
            }
        }
    }))
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

pub(crate) fn maybe_generate(
    generate: &mut Option<&mut TextGenerator<'_>>,
    prompt: &str,
    system: &str,
) -> Option<String> {
    generate
        .as_deref_mut()
        .and_then(|generate| generate(prompt, system))
        .and_then(clean_generated)
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

pub(crate) fn citation_list(spans: &[SourceSpan]) -> String {
    spans
        .iter()
        .cloned()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .map(|span| span.citation())
        .collect::<Vec<_>>()
        .join(" ")
}

pub(crate) fn ground_text(
    text: &str,
    valid_spans: &[SourceSpan],
    fallback_citation: &str,
) -> String {
    let cleaned = strip_invalid_citations(text, valid_spans);
    if fallback_citation.is_empty() || contains_valid_citation(&cleaned, valid_spans) {
        cleaned
    } else {
        format!("{cleaned} {fallback_citation}")
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

pub(crate) fn frontmatter(title: &str, kind: &str, source_spans: &[SourceSpan]) -> String {
    let mut out = format!("---\ntitle: \"{}\"\ntype: {kind}\n", yaml_quote(title));
    let mut files: BTreeMap<&str, BTreeSet<(usize, usize)>> = BTreeMap::new();
    for span in source_spans {
        files
            .entry(&span.file)
            .or_default()
            .insert((span.line_start, span.line_end));
    }
    if files.is_empty() {
        out.push_str("source_files: []\n");
        out.push_str("---\n\n");
        return out;
    }
    out.push_str("source_files:\n");
    for (file, ranges) in files {
        let _ = writeln!(out, "  - file: \"{}\"", yaml_quote(file));
        out.push_str("    ranges:\n");
        for (line_start, line_end) in ranges {
            if line_start == line_end {
                let _ = writeln!(out, "      - \"{line_start}\"");
            } else {
                let _ = writeln!(out, "      - \"{line_start}-{line_end}\"");
            }
        }
    }
    out.push_str("---\n\n");
    out
}

pub(crate) fn yaml_quote(value: &str) -> String {
    value.replace('\\', "\\\\").replace('"', "\\\"")
}
