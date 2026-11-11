use super::{from_slice_nested, MAX_NESTING};
use serde_json::Value;

fn nested_array(depth: usize) -> String {
    "[".repeat(depth) + &"]".repeat(depth)
}

#[test]
fn document_past_the_default_depth_decodes() {
    let document = nested_array(200);
    assert!(from_slice_nested::<Value>(document.as_bytes()).is_ok());
}

#[test]
fn document_beyond_the_nesting_limit_is_rejected() {
    let document = nested_array(MAX_NESTING + 1);
    assert!(from_slice_nested::<Value>(document.as_bytes()).is_err());
}

#[test]
fn brackets_inside_string_literals_do_not_count_toward_nesting() {
    let brackets = "[".repeat(MAX_NESTING + 10);
    let document = format!(r#"["quoted \" {brackets}"]"#);
    assert!(from_slice_nested::<Value>(document.as_bytes()).is_ok());
}

#[test]
fn trailing_bytes_after_a_deep_document_are_rejected() {
    let document = nested_array(200) + " x";
    assert!(from_slice_nested::<Value>(document.as_bytes()).is_err());
}
