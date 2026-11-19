//! The one encoder against Python's `json.dumps`, byte for byte.
//!
//! Each expected string was produced by `python3 -I` from the expression in the comment
//! above it.

use gobby_core::json::{
    CANONICAL, COMPACT, DumpError, HTTP, Json, PYTHON, RAW_CANONICAL, dumps, python_float_repr,
    quote,
};
use serde_json::json;

fn text(value: &str) -> Json {
    Json::Str(value.to_string())
}

fn int(digits: &str) -> Json {
    Json::Int(digits.to_string())
}

fn object(entries: &[(&str, Json)]) -> Json {
    Json::Object(
        entries
            .iter()
            .map(|(key, value)| (key.to_string(), value.clone()))
            .collect(),
    )
}

/// `{"a": "é", "rocket": "\U0001F680", "del": "x\x7fy", "ctl": "\x01\x1f\t\n\r\b\f\"\\"}`
fn ascii_case() -> Json {
    object(&[
        ("a", text("é")),
        ("rocket", text("\u{1F680}")),
        ("del", text("x\u{7f}y")),
        ("ctl", text("\u{1}\u{1f}\t\n\r\u{8}\u{c}\"\\")),
    ])
}

/// `{"tty": "/dev/ttyé", "term": "\U0001F680"}`
fn raw_case() -> Json {
    object(&[("tty", text("/dev/ttyé")), ("term", text("\u{1F680}"))])
}

fn encoded(value: &Json, style: gobby_core::json::Style) -> Result<String, DumpError> {
    dumps(value, style)
}

fn ok(expected: &str) -> Result<String, DumpError> {
    Ok(expected.to_string())
}

#[test]
fn python_default_style_matches_json_dumps() {
    // json.dumps(ascii_case)
    assert_eq!(
        encoded(&ascii_case(), PYTHON),
        ok(
            r#"{"a": "\u00e9", "rocket": "\ud83d\ude80", "del": "x\u007fy", "ctl": "\u0001\u001f\t\n\r\b\f\"\\"}"#
        )
    );
    // json.dumps([1, 2.5, None, True, False, -0.0, 1.0])
    let list = Json::Array(vec![
        int("1"),
        Json::Float(2.5),
        Json::Null,
        Json::Bool(true),
        Json::Bool(false),
        Json::Float(-0.0),
        Json::Float(1.0),
    ]);
    assert_eq!(
        encoded(&list, PYTHON),
        ok("[1, 2.5, null, true, false, -0.0, 1.0]")
    );
    // json.dumps([1e16, 1e-5, 0.0001, 1.1805916207174113e21, 123456789.0, 1e15, 2.5e-7, 1.5e300])
    let floats = Json::Array(
        [
            1e16,
            1e-5,
            0.0001,
            1.1805916207174113e21,
            123456789.0,
            1e15,
            2.5e-7,
            1.5e300,
        ]
        .into_iter()
        .map(Json::Float)
        .collect(),
    );
    assert_eq!(
        encoded(&floats, PYTHON),
        ok(
            "[1e+16, 1e-05, 0.0001, 1.1805916207174113e+21, 123456789.0, 1000000000000000.0, 2.5e-07, 1.5e+300]"
        )
    );
    // json.dumps(raw_case)
    assert_eq!(
        encoded(&raw_case(), PYTHON),
        ok(r#"{"tty": "/dev/tty\u00e9", "term": "\ud83d\ude80"}"#)
    );
}

#[test]
fn canonical_sorts_and_escapes_like_sort_keys_dump() {
    // json.dumps(ascii_case, sort_keys=True, separators=(",", ":"))
    assert_eq!(
        encoded(&ascii_case(), CANONICAL),
        ok(
            r#"{"a":"\u00e9","ctl":"\u0001\u001f\t\n\r\b\f\"\\","del":"x\u007fy","rocket":"\ud83d\ude80"}"#
        )
    );
}

#[test]
fn raw_canonical_keeps_utf8_and_sorts() {
    // json.dumps(raw_case, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert_eq!(
        encoded(&raw_case(), RAW_CANONICAL),
        ok(r#"{"term":"🚀","tty":"/dev/ttyé"}"#)
    );
}

#[test]
fn http_style_keeps_insertion_order_and_utf8() {
    // json.dumps(raw_case, ensure_ascii=False, separators=(",", ":"))
    assert_eq!(
        encoded(&raw_case(), HTTP),
        ok(r#"{"tty":"/dev/ttyé","term":"🚀"}"#)
    );
    // json.dumps(ascii_case, ensure_ascii=False, separators=(",", ":")): DEL stays a raw
    // byte; the C0 controls are still escaped.
    let expected = concat!(
        r#"{"a":"é","rocket":"🚀","del":"x"#,
        "\u{7f}",
        r#"y","ctl":"\u0001\u001f\t\n\r\b\f\"\\"}"#
    );
    assert_eq!(encoded(&ascii_case(), HTTP), ok(expected));
}

#[test]
fn compact_style_escapes_ascii_keeps_order() {
    // json.dumps(raw_case, separators=(",", ":"))
    assert_eq!(
        encoded(&raw_case(), COMPACT),
        ok(r#"{"tty":"/dev/tty\u00e9","term":"\ud83d\ude80"}"#)
    );
}

#[test]
fn python_float_repr_table() {
    // repr(f) for each f
    let table = [
        (1e16, "1e+16"),
        (1e-5, "1e-05"),
        (0.0001, "0.0001"),
        (1e15, "1000000000000000.0"),
        (2.5e-7, "2.5e-07"),
        (1.5e300, "1.5e+300"),
        (1.1805916207174113e21, "1.1805916207174113e+21"),
        (-0.0, "-0.0"),
        (1.0, "1.0"),
        (5e-324, "5e-324"),
    ];
    let rendered: Vec<(f64, String)> = table
        .iter()
        .map(|(value, _)| (*value, python_float_repr(*value)))
        .collect();
    let expected: Vec<(f64, String)> = table
        .iter()
        .map(|(value, repr)| (*value, repr.to_string()))
        .collect();
    assert_eq!(rendered, expected);
}

#[test]
fn non_finite_follows_allow_nan() {
    let values = Json::Array(vec![
        Json::Float(f64::NAN),
        Json::Float(f64::INFINITY),
        Json::Float(f64::NEG_INFINITY),
    ]);
    // json.dumps([float("nan"), float("inf"), float("-inf")])
    assert_eq!(encoded(&values, PYTHON), ok("[NaN, Infinity, -Infinity]"));
    for style in [COMPACT, CANONICAL, RAW_CANONICAL] {
        assert_eq!(encoded(&values, style), ok("[NaN,Infinity,-Infinity]"));
    }
    // json.dumps([float("nan")], allow_nan=False) raises ValueError
    assert_eq!(
        encoded(&Json::Array(vec![Json::Float(f64::NAN)]), HTTP),
        Err(DumpError::NonFinite)
    );
}

#[test]
fn parse_follows_json_loads() {
    // json.dumps(json.loads('{"a":1,"b":2,"a":3}'))
    let repeated = Json::parse(r#"{"a":1,"b":2,"a":3}"#).expect("valid JSON");
    assert_eq!(repeated, object(&[("a", int("3")), ("b", int("2"))]));
    assert_eq!(encoded(&repeated, PYTHON), ok(r#"{"a": 3, "b": 2}"#));
    // json.dumps(json.loads("-0"))
    let zero = Json::parse("-0").expect("valid JSON");
    assert_eq!(encoded(&zero, PYTHON), ok("0"));
    // json.dumps(json.loads("123456789012345678901234567890"))
    let big = Json::parse("123456789012345678901234567890").expect("valid JSON");
    assert_eq!(big, int("123456789012345678901234567890"));
    assert_eq!(encoded(&big, PYTHON), ok("123456789012345678901234567890"));
}

#[test]
fn quote_matches_json_dumps_string() {
    // json.dumps("é")
    assert_eq!(quote("é", true), r#""\u00e9""#);
    // json.dumps("é", ensure_ascii=False)
    assert_eq!(quote("é", false), "\"é\"");
    // json.dumps("\x7f")
    assert_eq!(quote("\u{7f}", true), r#""\u007f""#);
}

#[test]
fn from_value_keeps_number_kinds() {
    assert_eq!(Json::from(&json!(1)), int("1"));
    assert_eq!(Json::from(&json!(-5)), int("-5"));
    assert_eq!(Json::from(&json!(u64::MAX)), int("18446744073709551615"));
    assert_eq!(Json::from(&json!(1.0)), Json::Float(1.0));
    assert_eq!(
        Json::from(&json!(["x", null, true])),
        Json::Array(vec![text("x"), Json::Null, Json::Bool(true)])
    );
}
