//! Byte contract for `canonical_json`: Python's `json.dumps(sort_keys=True,
//! separators=(",", ":"))` escapes every character outside printable ASCII, so the
//! Rust settings bytes must match it for Unicode and non-BMP paths.

use super::canonical_json;
use serde_json::json;

#[test]
fn canonical_json_escapes_non_ascii_like_python() {
    let value = json!({
        "caf\u{e9}": "/tmp/caf\u{e9}",
        "k\u{1F600}": "/tmp/\u{1F600}\u{7f}\t\"\\"
    });

    // `\x5c` is a literal backslash, so the expected escape sequences stay ASCII.
    let expected = concat!(
        "{\"caf\x5cu00e9\":\"/tmp/caf\x5cu00e9\",",
        "\"k\x5cud83d\x5cude00\":\"/tmp/\x5cud83d\x5cude00\x5cu007f\\t\\\"\\\\\"}",
    );
    assert_eq!(canonical_json(&value), expected);
}
