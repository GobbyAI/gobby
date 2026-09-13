use super::*;

#[test]
fn markdown_extensions_are_not_detected() {
    // Markdown is intentionally handled as content-only text, not AST.
    assert_eq!(detect_language("README.md"), None);
    assert_eq!(detect_language("docs/guide.markdown"), None);
}

#[test]
fn supported_language_uses_detection_registry() {
    assert!(is_supported_language("src/lib.rs"));
    assert!(!is_supported_language("docs/guide.md"));
}

#[test]
fn javascript_extensions_still_detect() {
    assert_eq!(detect_language("src/app.js"), Some("javascript"));
    assert_eq!(detect_language("src/app.jsx"), Some("javascript"));
    assert_eq!(detect_language("src/app.cjs"), Some("javascript"));
    assert_eq!(detect_language("src/generated.mjs"), Some("javascript"));
}

#[test]
fn typescript_extensions_still_detect() {
    assert_eq!(detect_language("src/app.ts"), Some("typescript"));
    assert_eq!(detect_language("src/app.tsx"), Some("typescript"));
}

#[test]
fn bash_extensions_detect() {
    assert_eq!(detect_language("scripts/deploy.sh"), Some("bash"));
    assert_eq!(detect_language("scripts/env.bash"), Some("bash"));
}

#[test]
fn scala_extensions_detect() {
    assert_eq!(detect_language("src/main/scala/App.scala"), Some("scala"));
    assert_eq!(detect_language("scripts/build.sc"), Some("scala"));
}

#[test]
fn lua_extensions_detect() {
    assert_eq!(detect_language("lua/app/init.lua"), Some("lua"));
}

#[test]
fn objc_extensions_detect() {
    assert_eq!(detect_language("Sources/App/Widget.m"), Some("objc"));
    assert_eq!(detect_language("Sources/App/Widget.mm"), Some("objc"));
}

#[test]
fn c_header_detects_without_objc_or_cpp_signal() {
    assert_eq!(detect_language("Sources/App/Widget.h"), Some("c"));
}

#[test]
fn objc_header_detects_declaration_signal() {
    let tempdir = tempfile::TempDir::new().expect("create tempdir");
    let header = tempdir.path().join("Widget.h");
    std::fs::write(
        &header,
        r#"
@interface Widget
- (void)render;
@end
"#,
    )
    .expect("write header");

    assert_eq!(detect_language(&header.to_string_lossy()), Some("objc"));
}

#[test]
fn objc_header_detects_sibling_implementation_signal() {
    let tempdir = tempfile::TempDir::new().expect("create tempdir");
    let header = tempdir.path().join("Widget.h");
    std::fs::write(&header, "void WidgetRender(void);\n").expect("write header");
    std::fs::write(
        tempdir.path().join("Widget.m"),
        "void WidgetRender(void) {}\n",
    )
    .expect("write implementation");

    assert_eq!(detect_language(&header.to_string_lossy()), Some("objc"));
}

#[test]
fn cpp_header_detects_cpp_signal() {
    let tempdir = tempfile::TempDir::new().expect("create tempdir");
    let header = tempdir.path().join("Widget.h");
    std::fs::write(
        &header,
        r#"
namespace app {
template <typename T>
class Widget {};
}
"#,
    )
    .expect("write header");

    assert_eq!(detect_language(&header.to_string_lossy()), Some("cpp"));
}

#[test]
fn objcxx_paths_use_objc_grammar() {
    let language = get_ts_language_for_path("objc", "Sources/App/Widget.mm").unwrap();
    assert!(parses_without_error(
        language,
        r#"
@interface Widget
- (void)render;
@end

@implementation Widget
- (void)render { helper(); }
@end
"#,
    ));
}

#[test]
fn tsx_paths_use_tsx_grammar() {
    let language = get_ts_language_for_path("typescript", "src/app.tsx").unwrap();
    assert!(parses_without_error(
        language,
        "export const View = () => <section data-id=\"x\" />;",
    ));
}

#[test]
fn ts_paths_keep_typescript_grammar() {
    let language = get_ts_language_for_path("typescript", "src/app.ts").unwrap();
    assert!(parses_with_error(
        language,
        "export const View = () => <section />;"
    ));
}

fn parses_without_error(language: Language, source: &str) -> bool {
    let mut parser = tree_sitter::Parser::new();
    parser.set_language(&language).unwrap();
    let tree = parser.parse(source, None).unwrap();
    !tree.root_node().has_error()
}

fn parses_with_error(language: Language, source: &str) -> bool {
    let mut parser = tree_sitter::Parser::new();
    parser.set_language(&language).unwrap();
    let tree = parser.parse(source, None).unwrap();
    tree.root_node().has_error()
}

#[test]
fn is_data_language_matches_only_json_and_yaml() {
    assert!(is_data_language("json"));
    assert!(is_data_language("yaml"));
    // AST languages carry import/call queries and must not be treated as data.
    assert!(!is_data_language("rust"));
    assert!(!is_data_language("python"));
    // Dart has an empty call_query but a non-empty import_query, so it is excluded.
    assert!(!is_data_language("dart"));
    // Unknown languages are not data languages.
    assert!(!is_data_language("not_a_language"));
}
