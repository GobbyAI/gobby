//! The link under a ctrl+click: the OSC 8 target the cell carries, otherwise
//! the bare `http://` or `https://` run around the column (herdr
//! `url_at_pane_cell` and `url_at_column`). Only web URLs resolve: terminal
//! content is untrusted, so a `file:` or custom-scheme target never reaches
//! the opener.

use gobby_terminal::protocol::CellData;

use crate::app::PaneId;
use crate::ui::WorkspaceView;

use super::select::frame_row;

/// The URL a ctrl+click on frame cell (`row`, `col`) of `pane` opens, if any.
pub(super) fn resolve<W: WorkspaceView>(
    ws: &W,
    pane: PaneId,
    row: u16,
    col: u16,
) -> Option<String> {
    let frame = ws.pane(pane).latest_frame()?;
    let cells = frame_row(frame, row)?;
    let col = usize::from(col);
    // A wide character's continuation cell belongs to the cell before it.
    let owner = cells.get(col).map(|cell| {
        if cell.skip {
            col.saturating_sub(1)
        } else {
            col
        }
    })?;
    cells[owner]
        .hyperlink
        .and_then(|index| frame.hyperlinks.get(usize::try_from(index).ok()?))
        .filter(|uri| is_web_url(uri))
        .cloned()
        .or_else(|| bare_url(cells, owner))
}

fn is_web_url(uri: &str) -> bool {
    uri.starts_with("http://") || uri.starts_with("https://")
}

/// The bare URL whose run covers `col`: the maximal run of non-blank cells
/// that starts with `http://` or `https://`, minus a trailing tail of quotes,
/// `.,;:!?>` and closing brackets nothing inside the URL opened (herdr
/// `url_spans` and `trim_url_edges`). `None` when `col` lands outside every
/// URL, trimmed tail included.
fn bare_url(cells: &[CellData], col: usize) -> Option<String> {
    let mut start = 0;
    while start < cells.len() {
        if !starts_with(cells, start, "http://") && !starts_with(cells, start, "https://") {
            start += 1;
            continue;
        }
        let mut end = start;
        while cells
            .get(end + 1)
            .is_some_and(|cell| cell.skip || !cell.symbol.trim().is_empty())
        {
            end += 1;
        }
        if col < start || col > end {
            start = end + 1;
            continue;
        }
        let end = trimmed_end(cells, start, end)?;
        return (col <= end).then(|| {
            cells[start..=end]
                .iter()
                .map(|cell| cell.symbol.as_str())
                .collect()
        });
    }
    None
}

fn starts_with(cells: &[CellData], start: usize, prefix: &str) -> bool {
    prefix.chars().enumerate().all(|(offset, expected)| {
        cells
            .get(start + offset)
            .and_then(single_char)
            .is_some_and(|found| found == expected)
    })
}

fn single_char(cell: &CellData) -> Option<char> {
    let mut chars = cell.symbol.chars();
    let first = chars.next()?;
    chars.next().is_none().then_some(first)
}

/// Last cell of the URL once its trailing punctuation is dropped; `None`
/// when nothing survives.
fn trimmed_end(cells: &[CellData], start: usize, mut end: usize) -> Option<usize> {
    while trims(cells, start, end) {
        if end == start {
            return None;
        }
        end -= 1;
    }
    Some(end)
}

fn trims(cells: &[CellData], start: usize, end: usize) -> bool {
    match single_char(&cells[end]) {
        Some('"' | '\'' | '`' | '.' | ',' | ';' | ':' | '!' | '?' | '>') => true,
        Some(')') => !closes_open_bracket(cells, start, end, '(', ')'),
        Some(']') => !closes_open_bracket(cells, start, end, '[', ']'),
        Some('}') => !closes_open_bracket(cells, start, end, '{', '}'),
        _ => false,
    }
}

/// Whether the URL before `end` has an `open` bracket that `close` at `end`
/// pairs with, which makes the bracket part of the URL.
fn closes_open_bracket(
    cells: &[CellData],
    start: usize,
    end: usize,
    open: char,
    close: char,
) -> bool {
    let depth = cells[start..end]
        .iter()
        .filter_map(single_char)
        .fold(0i32, |depth, ch| match ch {
            _ if ch == open => depth + 1,
            _ if ch == close => depth - 1,
            _ => depth,
        });
    depth > 0
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cells(row: &str) -> Vec<CellData> {
        row.chars()
            .map(|symbol| CellData {
                symbol: symbol.to_string(),
                fg: 0,
                bg: 0,
                modifier: 0,
                skip: false,
                hyperlink: None,
            })
            .collect()
    }

    fn url_at(row: &str, needle: &str) -> Option<String> {
        let col = row
            .find(needle)
            .unwrap_or_else(|| panic!("{needle:?} in {row:?}"));
        bare_url(&cells(row), row[..col].chars().count())
    }

    #[test]
    fn bare_url_follows_herdr_trimming() {
        let cases = [
            (
                "see https://example.com/a-b_c?q=x@y.",
                "example.com",
                "https://example.com/a-b_c?q=x@y",
            ),
            (
                "open \"https://example.com/a,b;c?q=x\";",
                "example.com",
                "https://example.com/a,b;c?q=x",
            ),
            (
                "see https://en.wikipedia.org/wiki/Foo_(bar_(baz)),",
                "wikipedia",
                "https://en.wikipedia.org/wiki/Foo_(bar_(baz))",
            ),
            (
                "see https://example.com/a(b[c{d}e]f),",
                "example.com",
                "https://example.com/a(b[c{d}e]f)",
            ),
            (
                "see (https://example.com/a(b(c)d)))",
                "example.com",
                "https://example.com/a(b(c)d)",
            ),
            (
                "<https://example.com/x>",
                "example",
                "https://example.com/x",
            ),
            ("http://h/ then https://k/", "k/", "https://k/"),
        ];
        for (row, click, expected) in cases {
            assert_eq!(
                url_at(row, click).as_deref(),
                Some(expected),
                "row={row:?} click={click:?}"
            );
        }
    }

    #[test]
    fn bare_url_ignores_clicks_outside_the_trimmed_url() {
        assert_eq!(url_at("see https://example.com/x. next", "see"), None);
        assert_eq!(url_at("see https://example.com/x. next", ". next"), None);
        assert_eq!(url_at("see https://example.com/x. next", "next"), None);
        assert_eq!(url_at("ftp://example.com/x", "example"), None);
        // herdr scans every column, so a glued prefix still yields the URL.
        assert_eq!(
            url_at("nothttps://example.com/x", "example").as_deref(),
            Some("https://example.com/x")
        );
        assert_eq!(bare_url(&cells("short"), 40), None);
    }
}
