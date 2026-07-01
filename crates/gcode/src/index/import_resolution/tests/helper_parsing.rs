use super::common::*;

#[test]
fn quoted_string_ignores_escaped_quote_terminators() {
    assert_eq!(
        extract_quoted_string(r#"import "pkg:\"quoted\"/thing";"#).as_deref(),
        Some(r#"pkg:\"quoted\"/thing"#)
    );
}

#[test]
fn quoted_template_skips_interpolation_backticks() {
    assert_eq!(
        extract_quoted_string(r#"import `./${name ? `inner` : "fallback"}/view.js`"#).as_deref(),
        Some(r#"./${name ? `inner` : "fallback"}/view.js"#)
    );
}

#[test]
fn go_default_package_alias_uses_last_segment_before_version_suffix() {
    assert_eq!(go_default_package_alias("gopkg.in/yaml.v3"), "yaml");
    assert_eq!(
        go_default_package_alias("example.com/api.vnext"),
        "api.vnext"
    );
    assert_eq!(go_default_package_alias("example.com/api.vx"), "api.vx");
    assert_eq!(
        go_default_package_alias("github.com/acme/api-client/"),
        "api_client"
    );
}

#[test]
fn split_top_level_ignores_delimiters_inside_quotes_and_groups() {
    assert_eq!(
        split_top_level(r#"one, call("two, three"), map[a, b], {c, d}"#, ',')
            .expect("balanced split"),
        vec!["one", r#"call("two, three")"#, "map[a, b]", "{c, d}"]
    );
}

#[test]
fn split_top_level_preserves_escaped_quotes_inside_strings() {
    assert_eq!(
        split_top_level(r#"first, "two, \"still string\"", third"#, ',').expect("balanced split"),
        vec!["first", r#""two, \"still string\"""#, "third"]
    );
}

#[test]
fn split_top_level_rejects_unbalanced_delimiters() {
    let opening = split_top_level("one, call(two, three", ',')
        .expect_err("unbalanced opening delimiter should fail")
        .to_string();
    assert!(opening.contains("splitting on `,`"));
    assert!(opening.contains("one, call(two, three"));

    let closing = split_top_level("one, two]", ',')
        .expect_err("unbalanced closing delimiter should fail")
        .to_string();
    assert!(closing.contains("unbalanced closing bracket"));
    assert!(closing.contains("byte 8"));
}
