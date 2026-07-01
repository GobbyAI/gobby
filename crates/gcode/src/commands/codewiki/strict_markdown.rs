use std::collections::BTreeMap;

const LINE_LIMIT: usize = 80;

#[derive(Clone, Copy)]
struct MarkdownFence {
    marker: u8,
    len: usize,
}

enum Segment {
    Outside(Vec<String>),
    Fence(Vec<String>),
}

pub(crate) fn normalize_codewiki_markdown(input: &str) -> String {
    let shared = gobby_core::markdown::normalize_markdown(input);
    let body_start = frontmatter_body_start(&shared).unwrap_or(0);
    let prefix = &shared[..body_start];
    let body = normalize_body(&shared[body_start..]);

    let mut output = String::with_capacity(shared.len());
    output.push_str(prefix);
    if !body.is_empty() {
        if !prefix.is_empty() {
            output.push('\n');
        }
        output.push_str(&body);
    }
    while output.ends_with('\n') {
        output.pop();
    }
    if !output.is_empty() {
        output.push('\n');
    }
    output
}

fn normalize_body(body: &str) -> String {
    let mut out = Vec::new();
    for segment in split_fence_segments(body) {
        match segment {
            Segment::Outside(lines) => out.extend(normalize_outside_lines(&lines)),
            Segment::Fence(lines) => {
                push_blank(&mut out);
                out.extend(lines);
                push_blank(&mut out);
            }
        }
    }
    while out.last().is_some_and(|line| line.is_empty()) {
        out.pop();
    }
    out.join("\n")
}

fn split_fence_segments(body: &str) -> Vec<Segment> {
    let mut segments = Vec::new();
    let mut outside = Vec::new();
    let mut fence_lines = Vec::new();
    let mut fence = None;

    for raw in body.split('\n') {
        if let Some(active) = fence {
            fence_lines.push(raw.to_string());
            if markdown_fence_closes(raw, active) {
                segments.push(Segment::Fence(std::mem::take(&mut fence_lines)));
                fence = None;
            }
            continue;
        }

        let line = raw.trim_end();
        if let Some(opening) = markdown_fence_start(line) {
            if !outside.is_empty() {
                segments.push(Segment::Outside(std::mem::take(&mut outside)));
            }
            fence = Some(opening);
            fence_lines.push(line.to_string());
        } else {
            outside.push(line.to_string());
        }
    }

    if !fence_lines.is_empty() {
        segments.push(Segment::Fence(fence_lines));
    }
    if !outside.is_empty() {
        segments.push(Segment::Outside(outside));
    }
    segments
}

fn normalize_outside_lines(lines: &[String]) -> Vec<String> {
    let lines = strip_details_blocks(lines);
    let lines = convert_pipe_tables(&lines);
    let lines = normalize_headings(&lines);
    wrap_breakable_lines(&lines)
}

fn strip_details_blocks(lines: &[String]) -> Vec<String> {
    let mut out = Vec::with_capacity(lines.len());
    let mut in_details = false;
    for line in lines {
        let trimmed = line.trim_start();
        if in_details {
            if trimmed.contains("</details>") {
                in_details = false;
            }
            continue;
        }
        if trimmed.starts_with("<details") {
            in_details = !trimmed.contains("</details>");
            continue;
        }
        out.push(line.clone());
    }
    out
}

fn convert_pipe_tables(lines: &[String]) -> Vec<String> {
    let mut out = Vec::new();
    let mut index = 0;
    while index < lines.len() {
        let Some(headers) = parse_pipe_row(&lines[index]) else {
            out.push(lines[index].clone());
            index += 1;
            continue;
        };
        if index + 1 >= lines.len() || !is_table_separator(&lines[index + 1], headers.len()) {
            out.push(lines[index].clone());
            index += 1;
            continue;
        }

        let mut rows = Vec::new();
        index += 2;
        while index < lines.len() {
            let Some(row) = parse_pipe_row(&lines[index]) else {
                break;
            };
            rows.push(row);
            index += 1;
        }
        out.extend(table_rows_to_list(&headers, &rows));
    }
    out
}

fn table_rows_to_list(headers: &[String], rows: &[Vec<String>]) -> Vec<String> {
    let mut out = Vec::new();
    for row in rows {
        let mut emitted = false;
        for (cell_index, header) in headers.iter().enumerate() {
            let cell = row.get(cell_index).map(String::as_str).unwrap_or("").trim();
            if cell.is_empty() {
                continue;
            }
            let label = header.trim();
            let text = if label.is_empty() {
                cell.to_string()
            } else {
                format!("{label}: {cell}")
            };
            if emitted {
                out.push(format!("  {text}"));
            } else {
                out.push(format!("- {text}"));
                emitted = true;
            }
        }
        if !emitted {
            out.push("- empty row".to_string());
        }
    }
    out
}

fn parse_pipe_row(line: &str) -> Option<Vec<String>> {
    let trimmed = line.trim();
    if !trimmed.contains('|') {
        return None;
    }
    let inner = trimmed.strip_prefix('|').unwrap_or(trimmed);
    let inner = inner.strip_suffix('|').unwrap_or(inner);
    let cells = split_table_cells(inner)
        .into_iter()
        .map(|cell| cell.trim().to_string())
        .collect::<Vec<_>>();
    (cells.len() >= 2).then_some(cells)
}

fn split_table_cells(inner: &str) -> Vec<String> {
    let mut cells = Vec::new();
    let mut current = String::new();
    let mut bracket_depth = 0usize;
    let mut escaped = false;

    for ch in inner.chars() {
        if escaped {
            if ch == '|' {
                current.push('|');
            } else {
                current.push('\\');
                current.push(ch);
            }
            escaped = false;
            continue;
        }
        if ch == '\\' {
            escaped = true;
            continue;
        }
        match ch {
            '[' => bracket_depth += 1,
            ']' if bracket_depth > 0 => bracket_depth -= 1,
            '|' if bracket_depth == 0 => {
                cells.push(std::mem::take(&mut current));
                continue;
            }
            _ => {}
        }
        current.push(ch);
    }
    if escaped {
        current.push('\\');
    }
    cells.push(current);
    cells
}

fn is_table_separator(line: &str, header_len: usize) -> bool {
    let Some(cells) = parse_pipe_row(line) else {
        return false;
    };
    cells.len() == header_len
        && cells.iter().all(|cell| {
            let trimmed = cell.trim();
            trimmed.contains('-') && trimmed.chars().all(|ch| matches!(ch, '-' | ':' | ' '))
        })
}

fn normalize_headings(lines: &[String]) -> Vec<String> {
    let mut out = Vec::with_capacity(lines.len());
    let mut seen_h1 = false;
    let mut headings = BTreeMap::<String, usize>::new();
    for line in lines {
        let Some((mut level, title)) = parse_atx_heading(line) else {
            out.push(line.clone());
            continue;
        };
        if level == 1 {
            if seen_h1 {
                level = 2;
            } else {
                seen_h1 = true;
            }
        }
        let base = duplicate_heading_base(&title);
        let key = base.to_ascii_lowercase();
        let next = headings.entry(key).or_default();
        *next += 1;
        let title = if *next == 1 {
            title
        } else {
            format!("{base} ({})", *next)
        };
        out.push(format!(
            "{} {}",
            "#".repeat(usize::from(level)),
            escape_asterisk_math(&title)
        ));
    }
    out
}

fn duplicate_heading_base(title: &str) -> String {
    let trimmed = title.trim();
    let Some(prefix) = trimmed.strip_suffix(')') else {
        return trimmed.to_string();
    };
    let Some(open) = prefix.rfind(" (") else {
        return trimmed.to_string();
    };
    let number = &prefix[open + 2..];
    if number.parse::<usize>().is_ok_and(|value| value >= 2) {
        prefix[..open].trim_end().to_string()
    } else {
        trimmed.to_string()
    }
}

fn wrap_breakable_lines(lines: &[String]) -> Vec<String> {
    let mut out = Vec::new();
    let mut paragraph = Vec::new();
    let mut in_list = false;

    for line in lines {
        if line.trim().is_empty() {
            flush_paragraph(&mut paragraph, &mut out);
            push_blank(&mut out);
            in_list = false;
            continue;
        }
        if parse_atx_heading(line).is_some() {
            flush_paragraph(&mut paragraph, &mut out);
            push_blank(&mut out);
            out.push(line.clone());
            push_blank(&mut out);
            in_list = false;
            continue;
        }
        if let Some(item) = parse_unordered_list_item(line) {
            flush_paragraph(&mut paragraph, &mut out);
            if !in_list {
                push_blank(&mut out);
            }
            out.extend(wrap_line(
                &format!("{}- ", item.indent),
                &format!("{}  ", item.indent),
                item.text,
            ));
            in_list = true;
            continue;
        }
        if let Some(item) = parse_ordered_list_item(line) {
            flush_paragraph(&mut paragraph, &mut out);
            if !in_list {
                push_blank(&mut out);
            }
            let prefix = format!("{}{}. ", item.indent, item.number);
            out.extend(wrap_line(
                &prefix,
                &" ".repeat(prefix.chars().count()),
                item.text,
            ));
            in_list = true;
            continue;
        }
        if let Some(text) = line.strip_prefix("  ") {
            flush_paragraph(&mut paragraph, &mut out);
            out.extend(wrap_line("  ", "  ", text.trim_start()));
            in_list = true;
            continue;
        }
        if let Some((prefix, text)) = split_reference_definition(line) {
            flush_paragraph(&mut paragraph, &mut out);
            out.extend(wrap_line(prefix, "   ", text));
            in_list = false;
            continue;
        }

        if in_list {
            push_blank(&mut out);
            in_list = false;
        }
        paragraph.push(line.trim().to_string());
    }
    flush_paragraph(&mut paragraph, &mut out);
    while out.last().is_some_and(|line| line.is_empty()) {
        out.pop();
    }
    out
}

fn flush_paragraph(paragraph: &mut Vec<String>, out: &mut Vec<String>) {
    if paragraph.is_empty() {
        return;
    }
    let text = paragraph.join(" ");
    out.extend(wrap_line("", "", &text));
    paragraph.clear();
}

struct ListItem<'a> {
    indent: &'a str,
    text: &'a str,
}

struct OrderedListItem<'a> {
    indent: &'a str,
    number: &'a str,
    text: &'a str,
}

fn parse_unordered_list_item(line: &str) -> Option<ListItem<'_>> {
    let indent_len = line.len() - line.trim_start_matches(' ').len();
    let rest = &line[indent_len..];
    let marker = rest.as_bytes().first().copied()?;
    if !matches!(marker, b'-' | b'+' | b'*') || rest.as_bytes().get(1).copied() != Some(b' ') {
        return None;
    }
    Some(ListItem {
        indent: &line[..indent_len],
        text: rest[2..].trim(),
    })
}

fn parse_ordered_list_item(line: &str) -> Option<OrderedListItem<'_>> {
    let indent_len = line.len() - line.trim_start_matches(' ').len();
    let rest = &line[indent_len..];
    let marker_index = rest.find(|ch: char| !ch.is_ascii_digit())?;
    if marker_index == 0 {
        return None;
    }
    let marker = rest.as_bytes().get(marker_index).copied()?;
    if !matches!(marker, b'.' | b')') || rest.as_bytes().get(marker_index + 1) != Some(&b' ') {
        return None;
    }
    Some(OrderedListItem {
        indent: &line[..indent_len],
        number: &rest[..marker_index],
        text: rest[marker_index + 2..].trim(),
    })
}

fn split_reference_definition(line: &str) -> Option<(&str, &str)> {
    let marker = "]: ";
    let marker_index = line.find(marker)?;
    line.starts_with('[')
        .then_some(line.split_at(marker_index + marker.len()))
}

fn wrap_line(prefix: &str, continuation_prefix: &str, text: &str) -> Vec<String> {
    let text = escape_asterisk_math(text.trim());
    if prefix.chars().count() + text.chars().count() <= LINE_LIMIT {
        return vec![format!("{prefix}{text}")];
    }

    let units = wrap_units(&text);
    let mut out = Vec::new();
    let mut current = prefix.to_string();
    let mut current_len = prefix.chars().count();
    let prefix_len = prefix.chars().count();

    for unit in units {
        let unit_len = unit.chars().count();
        let needs_space = current_len > prefix_len;
        let projected = current_len + usize::from(needs_space) + unit_len;
        if projected > LINE_LIMIT && current_len > prefix_len {
            out.push(current);
            current = format!("{continuation_prefix}{unit}");
            current_len = continuation_prefix.chars().count() + unit_len;
        } else {
            if needs_space {
                current.push(' ');
                current_len += 1;
            }
            current.push_str(&unit);
            current_len += unit_len;
        }
    }
    if current_len > prefix_len || out.is_empty() {
        out.push(current);
    }
    out
}

fn wrap_units(text: &str) -> Vec<String> {
    let mut units = Vec::new();
    let mut current = String::new();
    let mut bracket_depth = 0usize;
    let mut paren_depth = 0usize;
    let mut in_code = false;

    for ch in text.chars() {
        if ch == '`' {
            in_code = !in_code;
        }
        if ch.is_whitespace() && bracket_depth == 0 && paren_depth == 0 && !in_code {
            if !current.is_empty() {
                units.push(std::mem::take(&mut current));
            }
            continue;
        }
        match ch {
            '[' if !in_code => bracket_depth += 1,
            ']' if !in_code && bracket_depth > 0 => bracket_depth -= 1,
            '(' if !in_code && !current.is_empty() && current.ends_with(']') => paren_depth += 1,
            ')' if !in_code && paren_depth > 0 => paren_depth -= 1,
            _ => {}
        }
        current.push(ch);
    }
    if !current.is_empty() {
        units.push(current);
    }
    units
}

fn escape_asterisk_math(text: &str) -> String {
    let chars = text.chars().collect::<Vec<_>>();
    let mut out = String::with_capacity(text.len());
    let mut in_code = false;
    for (index, ch) in chars.iter().copied().enumerate() {
        if ch == '`' {
            in_code = !in_code;
            out.push(ch);
            continue;
        }
        let previous = index.checked_sub(1).and_then(|idx| chars.get(idx)).copied();
        let next = chars.get(index + 1).copied();
        if ch == '*'
            && !in_code
            && previous.is_some_and(char::is_whitespace)
            && next.is_some_and(char::is_whitespace)
            && previous != Some('\\')
        {
            out.push('\\');
        }
        out.push(ch);
    }
    out
}

fn parse_atx_heading(line: &str) -> Option<(u8, String)> {
    let trimmed = line.trim_start();
    let level = trimmed.bytes().take_while(|byte| *byte == b'#').count();
    if level == 0 || level > 6 || trimmed.as_bytes().get(level) != Some(&b' ') {
        return None;
    }
    let title = strip_atx_closing_sequence(trimmed[level + 1..].trim()).trim();
    (!title.is_empty()).then(|| (level as u8, title.to_string()))
}

fn strip_atx_closing_sequence(title: &str) -> &str {
    let stripped = title.trim_end();
    let mut hash_start = stripped.len();
    for (index, ch) in stripped.char_indices().rev() {
        if ch == '#' {
            hash_start = index;
        } else {
            break;
        }
    }
    if hash_start < stripped.len()
        && stripped[..hash_start]
            .chars()
            .last()
            .is_some_and(char::is_whitespace)
    {
        stripped[..hash_start].trim_end()
    } else {
        stripped
    }
}

fn markdown_fence_start(line: &str) -> Option<MarkdownFence> {
    let leading_spaces = line.len() - line.trim_start_matches(' ').len();
    if leading_spaces > 3 {
        return None;
    }
    let trimmed = &line[leading_spaces..];
    let marker = match trimmed.as_bytes().first().copied()? {
        b'`' | b'~' => trimmed.as_bytes()[0],
        _ => return None,
    };
    let len = trimmed.bytes().take_while(|byte| *byte == marker).count();
    (len >= 3).then_some(MarkdownFence { marker, len })
}

fn markdown_fence_closes(line: &str, fence: MarkdownFence) -> bool {
    let leading_spaces = line.len() - line.trim_start_matches(' ').len();
    if leading_spaces > 3 {
        return false;
    }
    let trimmed = &line[leading_spaces..];
    let len = trimmed
        .bytes()
        .take_while(|byte| *byte == fence.marker)
        .count();
    len >= fence.len && trimmed[len..].trim().is_empty()
}

fn frontmatter_body_start(markdown: &str) -> Option<usize> {
    delimiter_content_start(markdown, "---")
        .and_then(|content_start| find_closing_delimiter(markdown, "---", content_start))
        .or_else(|| {
            delimiter_content_start(markdown, "+++")
                .and_then(|content_start| find_closing_delimiter(markdown, "+++", content_start))
        })
}

fn delimiter_content_start(markdown: &str, marker: &str) -> Option<usize> {
    markdown
        .strip_prefix(marker)?
        .strip_prefix('\n')
        .map(|rest| markdown.len() - rest.len())
}

fn find_closing_delimiter(markdown: &str, marker: &str, content_start: usize) -> Option<usize> {
    let mut offset = content_start;
    for line in markdown[content_start..].split_inclusive('\n') {
        let trimmed = line.trim_end_matches('\n').trim_end_matches('\r');
        if trimmed == marker {
            return Some(offset + line.len());
        }
        offset += line.len();
    }
    None
}

fn push_blank(out: &mut Vec<String>) {
    if out.last().is_some_and(|line| !line.is_empty()) {
        out.push(String::new());
    }
}

#[cfg(test)]
mod tests {
    use super::normalize_codewiki_markdown;

    #[test]
    fn preserves_frontmatter_byte_for_byte() {
        let input = "---\ntitle: A * B\nsource_files:\n  - src/lib.rs\n---\n\n# Page\n";
        let normalized = normalize_codewiki_markdown(input);

        assert!(normalized.starts_with("---\ntitle: A * B\nsource_files:\n  - src/lib.rs\n---\n"));
    }

    #[test]
    fn preserves_fenced_code_content_byte_for_byte() {
        let input = "# Page\n\n```rust\nfn main() {\n\n    println!(\"hi\");    \n}\n```   \n";
        let normalized = normalize_codewiki_markdown(input);

        assert!(normalized.contains("fn main() {\n\n    println!(\"hi\");    \n}\n```"));
        assert!(!normalized.contains("```   \n"));
    }

    #[test]
    fn strips_legacy_details_blocks() {
        let input = "# Page\n\nBefore\n\n<details>\n<summary>Source</summary>\n\nhidden\n</details>\n\nAfter\n";
        let normalized = normalize_codewiki_markdown(input);

        assert_eq!(normalized, "# Page\n\nBefore\n\nAfter\n");
    }

    #[test]
    fn keeps_one_h1_and_demotes_later_h1s() {
        let input = "# Page\n\n# Page\n\n# Other\n";
        let normalized = normalize_codewiki_markdown(input);

        assert_eq!(normalized, "# Page\n\n## Page (2)\n\n## Other\n");
    }

    #[test]
    fn suffixes_duplicate_headings_idempotently() {
        let input = "# Page\n\n## Flow\n\n## Flow\n\n### Flow (3)\n";
        let normalized = normalize_codewiki_markdown(input);

        assert_eq!(
            normalized,
            "# Page\n\n## Flow\n\n## Flow (2)\n\n### Flow (3)\n"
        );
        assert_eq!(normalize_codewiki_markdown(&normalized), normalized);
    }

    #[test]
    fn converts_pipe_tables_to_lists() {
        let input = "# Page\n\n| Reference | Summary |\n| --- | --- |\n| [[code/a]] | This row is intentionally short. |\n";
        let normalized = normalize_codewiki_markdown(input);

        assert_eq!(
            normalized,
            "# Page\n\n- Reference: [[code/a]]\n  Summary: This row is intentionally short.\n"
        );
    }

    #[test]
    fn wraps_prose_and_list_lines() {
        let input = "# Page\n\nThis generated sentence has many words that are safe to wrap without touching links or code spans in the final Markdown output.\n\n- This generated list item has many words that are safe to wrap without changing its meaning for markdownlint.\n";
        let normalized = normalize_codewiki_markdown(input);

        for line in normalized.lines().filter(|line| !line.starts_with("# ")) {
            assert!(line.len() <= 80, "{line}");
        }
        assert!(normalized.lines().any(|line| line.starts_with("  ")));
    }

    #[test]
    fn escapes_plain_text_asterisk_math() {
        let input = "# Page\n\nThe score is width * height while `width * height` stays literal.\n";
        let normalized = normalize_codewiki_markdown(input);

        assert!(normalized.contains("width \\* height"));
        assert!(normalized.contains("`width * height`"));
    }

    #[test]
    fn strict_normalization_is_idempotent() {
        let input = "---\ntitle: Page\n---\n\n# Page\n\n# Page\n\nA long generated sentence with width * height and enough additional prose to require wrapping for the default markdownlint line length rule.\n";
        let normalized = normalize_codewiki_markdown(input);

        assert_eq!(normalize_codewiki_markdown(&normalized), normalized);
    }
}
