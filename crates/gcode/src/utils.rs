pub fn short_id(id: &str) -> String {
    id.chars().take(8).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn short_id_truncates_long_ids() {
        assert_eq!(short_id("1234567890"), "12345678");
    }

    #[test]
    fn short_id_returns_input_for_short_strings() {
        assert_eq!(short_id("abc"), "abc");
    }

    #[test]
    fn short_id_returns_input_for_exact_length() {
        assert_eq!(short_id("12345678"), "12345678");
    }

    #[test]
    fn short_id_handles_unicode() {
        let value = "\u{00e9}".repeat(9);
        let expected = "\u{00e9}".repeat(8);
        assert_eq!(short_id(&value), expected);
    }
}
