use sha2::{Digest, Sha256};

pub fn api_key_fingerprint(api_key: &str) -> String {
    let digest = Sha256::digest(api_key.as_bytes());
    format!("{digest:x}").chars().take(16).collect()
}

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

    #[test]
    fn api_key_fingerprint_uses_stable_short_sha256() {
        assert_eq!(api_key_fingerprint("secret-key"), "85dbe15d75ef9308");
    }
}
