//! Checksum validation and at-rest hashing for public API-key bearers.

pub fn parse(key: &str) -> Option<&str> {
    const BASE62: &[u8; 62] = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";
    if key.len() != 55
        || !key.starts_with("gobby_")
        || !key.as_bytes()[6..].iter().all(u8::is_ascii_alphanumeric)
    {
        return None;
    }
    let mut checksum = crc32fast::hash(&key.as_bytes()[6..49]);
    let mut encoded = [b'0'; 6];
    for digit in encoded.iter_mut().rev() {
        *digit = BASE62[(checksum % 62) as usize];
        checksum /= 62;
    }
    openssl::memcmp::eq(&encoded, &key.as_bytes()[49..]).then_some(key)
}

pub fn hash(key: &str) -> String {
    openssl::sha::sha256(key.as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::{hash, parse};

    #[test]
    fn matches_shared_vectors() {
        // Shared with tests/utils/test_api_key_format.py (plan section 4.2).
        let vectors = [
            (
                "gobby_00000000000000000000000000000000000000000002CZclj",
                "a9f63698d531f354cfaa4434bab6790e56968528916f7a1be277d0d8d300b707",
            ),
            (
                "gobby_yhjskwdA6OZ1AL1YmHWZWm8LLG7HjnuCA2j5rOw8Xp13sRzl1",
                "52387dbd37befc4de42e256ed58cc5ac1f678918547901b6a22f79c18b92ea65",
            ),
            (
                "gobby_003aUlTJC7tjlCTQj2uNU3MFagCXG9LRKRcwGkBIDlf1Yo7hP",
                "5b285b61a8eb6a6c67f270bdf5cc6334bf9d0c63fe0fade7e744469d60f0c974",
            ),
        ];
        for (key, digest) in vectors {
            assert_eq!(parse(key), Some(key));
            assert_eq!(hash(key), digest);
        }
        let key = vectors[2].0;
        let bad_checksum = format!("{}Q", &key[..key.len() - 1]);
        let bad_alphabet = format!("{}-{}", &key[..10], &key[11..]);
        for invalid in [
            bad_checksum,
            bad_alphabet,
            key[..key.len() - 1].to_owned(),
            format!("{key}0"),
            format!("gobbi_{}", &key[6..]),
            String::new(),
            "gobby_😀".to_owned(),
        ] {
            assert_eq!(parse(&invalid), None, "{invalid}");
        }
    }
}
