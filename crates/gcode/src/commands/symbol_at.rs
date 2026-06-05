use std::cmp::Ordering;

use anyhow::{Context as AnyhowContext, anyhow, bail};
use serde::Serialize;

use super::scope;
use crate::{
    config::Context,
    db,
    models::Symbol,
    output::{self, Format},
    savings, visibility,
};

#[derive(Debug, Clone, PartialEq, Eq)]
struct ParsedLocation {
    file: String,
    line: usize,
    column: Option<usize>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct SymbolAtTarget {
    line: usize,
    byte_offset: Option<usize>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
enum MatchKind {
    Containing,
    Nearest,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
struct SymbolAtLookup {
    requested_file: String,
    requested_line: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    requested_column: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    column_unit: Option<&'static str>,
    match_kind: MatchKind,
    distance_lines: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    distance_bytes: Option<usize>,
}

#[derive(Debug, Clone, Copy)]
struct SelectedSymbol<'a> {
    symbol: &'a Symbol,
    match_kind: MatchKind,
    distance_lines: usize,
    distance_bytes: Option<usize>,
}

pub fn requested_file_for_freshness(
    ctx: &Context,
    location: &str,
    line: Option<usize>,
) -> anyhow::Result<String> {
    let parsed = parse_location(location, line)?;
    Ok(scope::normalize_file_arg(ctx, &parsed.file))
}

pub fn run(
    ctx: &Context,
    location: &str,
    line: Option<usize>,
    format: Format,
) -> anyhow::Result<()> {
    let request = parse_location(location, line)?;
    let requested_file = scope::normalize_file_arg(ctx, &request.file);
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let symbols = visibility::visible_symbols_for_file(&mut conn, ctx, &requested_file)?;
    if symbols.is_empty() {
        bail!("No visible symbols found for file: {requested_file}");
    }

    let source_path = ctx.project_root.join(&requested_file);
    let source = std::fs::read(&source_path)
        .with_context(|| format!("failed to read {}", source_path.display()))?;
    let byte_offset = request
        .column
        .map(|column| line_column_to_byte_offset(&source, request.line, column))
        .transpose()
        .with_context(|| {
            format!(
                "invalid location {}:{}:{}",
                requested_file,
                request.line,
                request.column.unwrap_or_default()
            )
        })?;

    let selected = select_symbol(
        &symbols,
        SymbolAtTarget {
            line: request.line,
            byte_offset,
        },
    )
    .ok_or_else(|| anyhow!("No visible symbols found for file: {requested_file}"))?;
    let lookup = lookup_for_selection(requested_file, &request, &selected);
    let (snippet, symbol_bytes) = symbol_source(&source, selected.symbol);

    if symbol_bytes > 0
        && source.len() > symbol_bytes
        && let Some(url) = savings::resolve_daemon_url(None)
    {
        savings::report_savings(&url, source.len(), symbol_bytes);
    }

    if let Some(diagnostic) = fallback_diagnostic(selected.symbol, &lookup, ctx.quiet) {
        eprintln!("{diagnostic}");
    }

    match format {
        Format::Json => {
            output::print_json(&symbol_at_json_value(selected.symbol, &snippet, &lookup)?)
        }
        Format::Text => output::print_text(&snippet),
    }
}

fn parse_location(location: &str, explicit_line: Option<usize>) -> anyhow::Result<ParsedLocation> {
    if location.is_empty() {
        bail!("path is required");
    }

    if let Some(line) = explicit_line {
        if line == 0 {
            bail!("line must be greater than 0");
        }
        if has_encoded_line(location) {
            bail!("line specified twice; use PATH:LINE or PATH LINE, not both");
        }
        return Ok(ParsedLocation {
            file: location.to_string(),
            line,
            column: None,
        });
    }

    let Some((prefix, last)) = location.rsplit_once(':') else {
        bail!("missing line; use PATH:LINE, PATH:LINE:COLUMN, or PATH LINE");
    };
    if prefix.is_empty() {
        bail!("path is required");
    }

    if let Some((path, line_text)) = prefix.rsplit_once(':')
        && is_numeric_text(line_text)
    {
        if path.is_empty() {
            bail!("path is required");
        }
        let line = parse_positive_component("line", line_text)?;
        let column = parse_positive_component("column", last)?;
        return Ok(ParsedLocation {
            file: path.to_string(),
            line,
            column: Some(column),
        });
    }

    let line = parse_positive_component("line", last)?;
    Ok(ParsedLocation {
        file: prefix.to_string(),
        line,
        column: None,
    })
}

fn has_encoded_line(location: &str) -> bool {
    let Some((prefix, last)) = location.rsplit_once(':') else {
        return false;
    };
    if is_numeric_text(last) {
        return true;
    }
    prefix
        .rsplit_once(':')
        .is_some_and(|(_, line)| is_numeric_text(line))
}

fn parse_positive_component(kind: &str, value: &str) -> anyhow::Result<usize> {
    let parsed = value
        .parse::<usize>()
        .map_err(|_| anyhow!("{kind} must be a positive integer"))?;
    if parsed == 0 {
        bail!("{kind} must be greater than 0");
    }
    Ok(parsed)
}

fn is_numeric_text(value: &str) -> bool {
    !value.is_empty() && value.bytes().all(|byte| byte.is_ascii_digit())
}

fn line_column_to_byte_offset(source: &[u8], line: usize, column: usize) -> anyhow::Result<usize> {
    if line == 0 {
        bail!("line must be greater than 0");
    }
    if column == 0 {
        bail!("column must be greater than 0");
    }

    let Some((start, end)) = line_bounds(source, line) else {
        bail!("line {line} is out of range");
    };
    let line_len = end.saturating_sub(start);
    if column > line_len {
        bail!("column {column} is out of range for line {line} ({line_len} bytes)");
    }
    Ok(start + column - 1)
}

fn line_bounds(source: &[u8], line: usize) -> Option<(usize, usize)> {
    let mut current_line = 1usize;
    let mut start = 0usize;
    for (index, byte) in source.iter().enumerate() {
        if *byte == b'\n' {
            if current_line == line {
                return Some((start, trim_cr(source, start, index)));
            }
            current_line += 1;
            start = index + 1;
        }
    }
    (current_line == line).then(|| (start, trim_cr(source, start, source.len())))
}

fn trim_cr(source: &[u8], start: usize, end: usize) -> usize {
    if end > start && source[end - 1] == b'\r' {
        end - 1
    } else {
        end
    }
}

fn select_symbol(symbols: &[Symbol], target: SymbolAtTarget) -> Option<SelectedSymbol<'_>> {
    if let Some(symbol) = symbols
        .iter()
        .filter(|symbol| contains_target(symbol, target))
        .min_by(|left, right| compare_containing(left, right))
    {
        return Some(SelectedSymbol {
            symbol,
            match_kind: MatchKind::Containing,
            distance_lines: 0,
            distance_bytes: target.byte_offset.map(|_| 0),
        });
    }

    symbols
        .iter()
        .min_by(|left, right| compare_nearest(left, right, target))
        .map(|symbol| SelectedSymbol {
            symbol,
            match_kind: MatchKind::Nearest,
            distance_lines: line_distance(symbol, target.line),
            distance_bytes: target
                .byte_offset
                .map(|offset| byte_distance(symbol, offset)),
        })
}

fn contains_target(symbol: &Symbol, target: SymbolAtTarget) -> bool {
    if let Some(offset) = target.byte_offset {
        return symbol.byte_start <= offset && offset < symbol.byte_end;
    }
    symbol.line_start <= target.line && target.line <= symbol.line_end
}

fn compare_containing(left: &Symbol, right: &Symbol) -> Ordering {
    line_span(left)
        .cmp(&line_span(right))
        .then_with(|| byte_span(left).cmp(&byte_span(right)))
        .then_with(|| right.byte_start.cmp(&left.byte_start))
}

fn compare_nearest(left: &Symbol, right: &Symbol, target: SymbolAtTarget) -> Ordering {
    line_distance(left, target.line)
        .cmp(&line_distance(right, target.line))
        .then_with(|| match target.byte_offset {
            Some(offset) => byte_distance(left, offset).cmp(&byte_distance(right, offset)),
            None => Ordering::Equal,
        })
        .then_with(|| compare_previous_preference(left, right, target))
}

fn compare_previous_preference(left: &Symbol, right: &Symbol, target: SymbolAtTarget) -> Ordering {
    match (
        is_previous_symbol(left, target),
        is_previous_symbol(right, target),
    ) {
        (true, false) => Ordering::Less,
        (false, true) => Ordering::Greater,
        (true, true) => right
            .line_end
            .cmp(&left.line_end)
            .then_with(|| right.byte_end.cmp(&left.byte_end))
            .then_with(|| right.byte_start.cmp(&left.byte_start)),
        (false, false) => left
            .line_start
            .cmp(&right.line_start)
            .then_with(|| left.byte_start.cmp(&right.byte_start)),
    }
}

fn is_previous_symbol(symbol: &Symbol, target: SymbolAtTarget) -> bool {
    if let Some(offset) = target.byte_offset {
        if symbol.byte_end <= offset {
            return true;
        }
        if symbol.byte_start > offset {
            return false;
        }
    }
    symbol.line_end < target.line
}

fn line_span(symbol: &Symbol) -> usize {
    symbol.line_end.saturating_sub(symbol.line_start)
}

fn byte_span(symbol: &Symbol) -> usize {
    symbol.byte_end.saturating_sub(symbol.byte_start)
}

fn line_distance(symbol: &Symbol, line: usize) -> usize {
    if line < symbol.line_start {
        symbol.line_start - line
    } else {
        line.saturating_sub(symbol.line_end)
    }
}

fn byte_distance(symbol: &Symbol, offset: usize) -> usize {
    if offset < symbol.byte_start {
        symbol.byte_start - offset
    } else if offset >= symbol.byte_end {
        offset.saturating_sub(symbol.byte_end)
    } else {
        0
    }
}

fn lookup_for_selection(
    requested_file: String,
    request: &ParsedLocation,
    selected: &SelectedSymbol<'_>,
) -> SymbolAtLookup {
    SymbolAtLookup {
        requested_file,
        requested_line: request.line,
        requested_column: request.column,
        column_unit: request.column.map(|_| "byte"),
        match_kind: selected.match_kind,
        distance_lines: selected.distance_lines,
        distance_bytes: selected.distance_bytes,
    }
}

fn symbol_source(source: &[u8], symbol: &Symbol) -> (String, usize) {
    let end = symbol.byte_end.min(source.len());
    let start = symbol.byte_start.min(end);
    let bytes = &source[start..end];
    (String::from_utf8_lossy(bytes).to_string(), bytes.len())
}

fn symbol_at_json_value(
    symbol: &Symbol,
    source: &str,
    lookup: &SymbolAtLookup,
) -> anyhow::Result<serde_json::Value> {
    let mut value = serde_json::to_value(symbol)?;
    value["source"] = serde_json::Value::String(source.to_string());
    value["lookup"] = serde_json::to_value(lookup)?;
    Ok(value)
}

fn fallback_diagnostic(symbol: &Symbol, lookup: &SymbolAtLookup, quiet: bool) -> Option<String> {
    if quiet || lookup.match_kind != MatchKind::Nearest {
        return None;
    }

    let requested = match lookup.requested_column {
        Some(column) => format!(
            "{}:{}:{}",
            lookup.requested_file, lookup.requested_line, column
        ),
        None => format!("{}:{}", lookup.requested_file, lookup.requested_line),
    };
    let mut distance = format!(
        "{} {}",
        lookup.distance_lines,
        plural("line", lookup.distance_lines)
    );
    if let Some(bytes) = lookup.distance_bytes {
        distance.push_str(&format!(", {bytes} {}", plural("byte", bytes)));
    }

    Some(format!(
        "gcode symbol-at: no symbol contains {requested}; using nearest visible symbol {}:{} [{}] {} ({distance} away)",
        symbol.file_path, symbol.line_start, symbol.kind, symbol.qualified_name
    ))
}

fn plural(unit: &'static str, value: usize) -> &'static str {
    if value == 1 {
        unit
    } else {
        match unit {
            "line" => "lines",
            "byte" => "bytes",
            _ => unit,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::Symbol;

    fn symbol(
        name: &str,
        line_start: usize,
        line_end: usize,
        byte_start: usize,
        byte_end: usize,
    ) -> Symbol {
        Symbol {
            id: format!("{name}-id"),
            project_id: "project".to_string(),
            file_path: "src/auth.ts".to_string(),
            name: name.to_string(),
            qualified_name: name.to_string(),
            kind: "function".to_string(),
            language: "typescript".to_string(),
            byte_start,
            byte_end,
            line_start,
            line_end,
            signature: Some(format!("function {name}()")),
            docstring: None,
            parent_symbol_id: None,
            content_hash: String::new(),
            summary: None,
            created_at: String::new(),
            updated_at: String::new(),
        }
    }

    fn error_text<T>(result: anyhow::Result<T>) -> String {
        match result {
            Ok(_) => panic!("expected error"),
            Err(error) => error.to_string(),
        }
    }

    #[test]
    fn parses_path_line_and_column_from_the_right() {
        let line = parse_location("src:auth.ts:42", None).expect("PATH:LINE parses");
        assert_eq!(line.file, "src:auth.ts");
        assert_eq!(line.line, 42);
        assert_eq!(line.column, None);

        let column = parse_location("src:auth.ts:42:7", None).expect("PATH:LINE:COLUMN parses");
        assert_eq!(column.file, "src:auth.ts");
        assert_eq!(column.line, 42);
        assert_eq!(column.column, Some(7));
    }

    #[test]
    fn parses_path_with_separate_line() {
        let parsed = parse_location("src:auth.ts", Some(42)).expect("PATH LINE parses");

        assert_eq!(parsed.file, "src:auth.ts");
        assert_eq!(parsed.line, 42);
        assert_eq!(parsed.column, None);
    }

    #[test]
    fn rejects_invalid_location_forms() {
        assert!(error_text(parse_location("src/auth.ts", None)).contains("missing line"));
        assert!(
            error_text(parse_location("src/auth.ts:42", Some(7))).contains("line specified twice")
        );
        assert!(
            error_text(parse_location("src/auth.ts:0", None))
                .contains("line must be greater than 0")
        );
        assert!(
            error_text(parse_location("src/auth.ts:42:0", None))
                .contains("column must be greater than 0")
        );
        assert!(
            error_text(parse_location("src/auth.ts:nope", None))
                .contains("line must be a positive integer")
        );
        assert!(
            error_text(parse_location("src/auth.ts:42:nope", None))
                .contains("column must be a positive integer")
        );
    }

    #[test]
    fn converts_one_based_byte_columns_to_offsets() {
        let source = "abc\néx\n".as_bytes();

        assert_eq!(line_column_to_byte_offset(source, 1, 1).unwrap(), 0);
        assert_eq!(line_column_to_byte_offset(source, 1, 3).unwrap(), 2);
        assert_eq!(line_column_to_byte_offset(source, 2, 1).unwrap(), 4);
        assert_eq!(line_column_to_byte_offset(source, 2, 2).unwrap(), 5);
        assert_eq!(line_column_to_byte_offset(source, 2, 3).unwrap(), 6);
    }

    #[test]
    fn rejects_out_of_range_columns() {
        assert!(
            error_text(line_column_to_byte_offset("abc\n".as_bytes(), 1, 4),)
                .contains("column 4 is out of range")
        );
    }

    #[test]
    fn containing_selection_prefers_smallest_span_then_later_start() {
        let outer = symbol("outer", 1, 10, 0, 100);
        let earlier = symbol("earlier", 4, 4, 20, 30);
        let later = symbol("later", 4, 4, 40, 50);
        let symbols = vec![outer, earlier, later];

        let selected = select_symbol(
            &symbols,
            SymbolAtTarget {
                line: 4,
                byte_offset: None,
            },
        )
        .expect("symbol selected");

        assert_eq!(selected.symbol.name, "later");
        assert_eq!(selected.match_kind, MatchKind::Containing);
        assert_eq!(selected.distance_lines, 0);
    }

    #[test]
    fn nearest_selection_prefers_previous_on_equal_line_distance() {
        let before = symbol("before", 2, 3, 20, 30);
        let after = symbol("after", 7, 8, 70, 80);
        let symbols = vec![before, after];

        let selected = select_symbol(
            &symbols,
            SymbolAtTarget {
                line: 5,
                byte_offset: None,
            },
        )
        .expect("symbol selected");

        assert_eq!(selected.symbol.name, "before");
        assert_eq!(selected.match_kind, MatchKind::Nearest);
        assert_eq!(selected.distance_lines, 2);
    }

    #[test]
    fn nearest_selection_uses_byte_distance_for_column_ties() {
        let left = symbol("left", 10, 10, 10, 20);
        let right = symbol("right", 10, 10, 24, 34);
        let symbols = vec![left, right];

        let selected = select_symbol(
            &symbols,
            SymbolAtTarget {
                line: 10,
                byte_offset: Some(23),
            },
        )
        .expect("symbol selected");

        assert_eq!(selected.symbol.name, "right");
        assert_eq!(selected.match_kind, MatchKind::Nearest);
        assert_eq!(selected.distance_lines, 0);
        assert_eq!(selected.distance_bytes, Some(1));
    }

    #[test]
    fn lookup_json_includes_source_and_metadata() {
        let sym = symbol("handler", 7, 9, 0, 12);
        let lookup = SymbolAtLookup {
            requested_file: "src/auth.ts".to_string(),
            requested_line: 12,
            requested_column: Some(3),
            column_unit: Some("byte"),
            match_kind: MatchKind::Nearest,
            distance_lines: 3,
            distance_bytes: Some(8),
        };

        let value = symbol_at_json_value(&sym, "source text", &lookup).expect("json builds");

        assert_eq!(value["name"], "handler");
        assert_eq!(value["source"], "source text");
        assert_eq!(value["lookup"]["requested_file"], "src/auth.ts");
        assert_eq!(value["lookup"]["requested_line"], 12);
        assert_eq!(value["lookup"]["requested_column"], 3);
        assert_eq!(value["lookup"]["column_unit"], "byte");
        assert_eq!(value["lookup"]["match_kind"], "nearest");
        assert_eq!(value["lookup"]["distance_lines"], 3);
        assert_eq!(value["lookup"]["distance_bytes"], 8);
    }

    #[test]
    fn nearest_diagnostic_is_suppressed_when_quiet_or_containing() {
        let sym = symbol("handler", 7, 9, 0, 12);
        let mut lookup = SymbolAtLookup {
            requested_file: "src/auth.ts".to_string(),
            requested_line: 12,
            requested_column: None,
            column_unit: None,
            match_kind: MatchKind::Nearest,
            distance_lines: 3,
            distance_bytes: None,
        };

        let diagnostic =
            fallback_diagnostic(&sym, &lookup, false).expect("nearest emits diagnostic");
        assert!(diagnostic.contains("using nearest visible symbol"));
        assert!(diagnostic.contains("src/auth.ts:7"));

        assert!(fallback_diagnostic(&sym, &lookup, true).is_none());

        lookup.match_kind = MatchKind::Containing;
        assert!(fallback_diagnostic(&sym, &lookup, false).is_none());
    }
}
