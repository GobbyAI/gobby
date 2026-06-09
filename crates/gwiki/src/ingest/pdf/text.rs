use crate::ingest::single_line;

pub(crate) fn normalize_page_text(text: &str) -> String {
    let mut paragraphs = Vec::new();
    let mut current = Vec::new();

    for line in text.lines() {
        let line = single_line(line);
        if line.is_empty() {
            if !current.is_empty() {
                paragraphs.push(current.join(" "));
                current.clear();
            }
            continue;
        }
        current.push(line);
    }

    if !current.is_empty() {
        paragraphs.push(current.join(" "));
    }

    paragraphs.join("\n\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalize_page_text_preserves_paragraph_breaks() {
        let text = normalize_page_text("First line\nwraps here.\n\nSecond paragraph.\n");

        assert_eq!(text, "First line wraps here.\n\nSecond paragraph.");
    }

    #[test]
    fn normalize_page_text_handles_whitespace_edges() {
        assert_eq!(normalize_page_text(" \t \n\n"), "");
        assert_eq!(
            normalize_page_text("First\t line\r\nwraps   here.\r\n\r\n\r\nSecond"),
            "First line wraps here.\n\nSecond"
        );
        assert_eq!(
            normalize_page_text("\n  First paragraph.  \n \n  Second paragraph.  \n"),
            "First paragraph.\n\nSecond paragraph."
        );
    }

    #[test]
    fn normalize_page_text_multiple_blank_lines() {
        assert_eq!(normalize_page_text("First\n\n\nSecond"), "First\n\nSecond");
    }

    #[test]
    fn normalize_page_text_trailing_blank_lines() {
        assert_eq!(normalize_page_text("First\n\n"), "First");
    }

    #[test]
    fn normalize_page_text_whitespace_only_lines() {
        assert_eq!(normalize_page_text(" \t \n  \n"), "");
    }

    #[test]
    fn normalize_page_text_empty_input() {
        assert_eq!(normalize_page_text(""), "");
    }

    #[test]
    fn normalize_page_text_single_line() {
        assert_eq!(normalize_page_text("Only line."), "Only line.");
    }

    #[test]
    fn normalize_page_text_no_blank_lines() {
        assert_eq!(
            normalize_page_text("First line\nwraps here\nand here"),
            "First line wraps here and here"
        );
    }
}
