//! Content hashing for incremental indexing.
//! Ports logic from src/gobby/code_index/hasher.py.

use std::path::Path;

/// SHA-256 hash of entire file contents.
pub fn file_content_hash(path: &Path) -> anyhow::Result<String> {
    Ok(gobby_core::indexing::file_content_hash(path)?)
}

/// SHA-256 hash of in-memory file contents.
pub fn content_hash(source: &[u8]) -> String {
    gobby_core::indexing::content_hash(source)
}

/// SHA-256 hash of a byte slice (symbol source).
pub fn symbol_content_hash(source: &[u8], start: usize, end: usize) -> anyhow::Result<String> {
    let slice = source.get(start..end).ok_or_else(|| {
        anyhow::anyhow!(
            "invalid byte range {}..{} for source len {}",
            start,
            end,
            source.len()
        )
    })?;
    Ok(gobby_core::indexing::content_hash(slice))
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    #[test]
    fn file_content_hash_delegates_to_gobby_core() {
        let tmp = tempfile::NamedTempFile::new().expect("tempfile");
        std::fs::write(tmp.path(), b"hash me\n").expect("write file");

        let actual = file_content_hash(tmp.path()).expect("hash via wrapper");
        let expected =
            gobby_core::indexing::file_content_hash(tmp.path()).expect("hash via gobby-core");
        assert_eq!(actual, expected);

        let source = include_str!("hasher.rs");
        let delegate = ["gobby_core", "::indexing::file_content_hash"].concat();
        let local_buffer = format!("let mut buf = [0u8; {}]", 64 * 1024);
        assert!(source.contains(&delegate));
        assert!(!source.contains(&local_buffer));
    }

    #[test]
    fn content_hash_delegates_to_gobby_core() {
        let source = b"hash me from memory\n";

        assert_eq!(
            content_hash(source),
            gobby_core::indexing::content_hash(source)
        );
    }

    proptest! {
        #[test]
        fn content_hash_matches_gobby_core_for_arbitrary_bytes(
            source in proptest::collection::vec(any::<u8>(), 0..4096),
        ) {
            prop_assert_eq!(
                content_hash(&source),
                gobby_core::indexing::content_hash(&source)
            );
        }

        #[test]
        fn symbol_content_hash_matches_gobby_core_for_valid_slices(
            source in proptest::collection::vec(any::<u8>(), 0..4096),
            raw_start in 0usize..4096,
            raw_len in 0usize..4096,
        ) {
            let start = raw_start.min(source.len());
            let end = (start + raw_len).min(source.len());
            let actual = symbol_content_hash(&source, start, end).expect("valid slice hashes");
            let expected = gobby_core::indexing::content_hash(&source[start..end]);

            prop_assert_eq!(actual, expected);
        }
    }
}
