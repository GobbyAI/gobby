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
}
