use super::common::*;
use std::fs;

#[test]
fn php_declared_symbols_ignores_comments_and_strings() {
    let symbols = php_declared_symbols(
        r#"
<?php
// class Fake {}
$message = "function hidden() {}";
class Real {}
function work() {}
"#,
    );

    assert!(symbols.contains(&"Real".to_string()));
    assert!(symbols.contains(&"work".to_string()));
    assert!(!symbols.contains(&"Fake".to_string()));
    assert!(!symbols.contains(&"hidden".to_string()));
}

#[test]
fn js_builtin_submodules_are_external_without_package_fallback() {
    let context = ImportResolutionContext::default();

    assert!(is_external_js_module("fs/promises", &context));
    assert!(is_external_js_module("stream/web", &context));
    assert!(!is_external_js_module("fs-extra/promises", &context));
}

#[test]
fn csharp_declared_types_includes_structs() {
    let names = csharp_declared_types(
        "public struct Point {} class Sample {} interface IThing {} enum Mode {} record Data;",
    );

    assert!(names.iter().any(|name| name == "Point"));
    assert!(names.iter().any(|name| name == "Sample"));
    assert!(names.iter().any(|name| name == "IThing"));
    assert!(names.iter().any(|name| name == "Mode"));
    assert!(names.iter().any(|name| name == "Data"));
}

#[test]
fn declared_type_scanner_ignores_comments_and_strings() {
    let names = java_declared_types(
        r#"
        // class FakeLine {}
        String value = "record FakeString";
        /* interface FakeBlock {} */
        public class RealType {}
        "#,
    );

    assert_eq!(names, vec!["RealType".to_string()]);
}

#[test]
fn java_inner_class_simple_name_can_be_local() {
    let mut import_context = ImportResolutionContext::default();
    import_context
        .java_local_classes
        .insert("Inner".to_string());

    assert!(!is_external_java_class(
        "com.example.Outer$Inner",
        &import_context
    ));
}

#[test]
fn node_v26_builtin_modules_are_external() {
    let import_context = ImportResolutionContext::default();

    assert!(is_external_js_module("stream/iter", &import_context));
    assert!(is_external_js_module("util/types", &import_context));
    assert!(is_external_js_module("zlib/iter", &import_context));
}

#[test]
fn empty_php_fully_qualified_namespace_stays_unresolved() {
    let target = resolve_external_callee(
        &ImportResolutionContext::default(),
        &ImportBindings::default(),
        &[],
        "helper",
        Some(""),
        Some("\\"),
        false,
    );

    assert!(target.is_none());
}

#[test]
fn php_local_fully_qualified_class_stays_unresolved() {
    let mut import_context = ImportResolutionContext::default();
    import_context
        .php_local_symbols
        .insert(r"app\services\mailer".to_string());

    let target = resolve_external_callee(
        &import_context,
        &ImportBindings::default(),
        &[],
        "send",
        Some("App"),
        Some(r"\App\Services\Mailer"),
        false,
    );

    assert!(target.is_none());
}

#[test]
fn php_local_fully_qualified_function_stays_unresolved() {
    let mut import_context = ImportResolutionContext::default();
    import_context
        .php_local_symbols
        .insert(r"app\helpers\render".to_string());

    let target = resolve_external_callee(
        &import_context,
        &ImportBindings::default(),
        &[],
        "render",
        Some("App"),
        Some(r"\App\Helpers"),
        false,
    );

    assert!(target.is_none());
}

#[test]
fn php_local_symbol_index_normalizes_declared_names_to_lowercase() {
    let tempdir = TempDir::new().expect("tempdir");
    let path = tempdir.path().join("Mailer.php");
    fs::write(
        &path,
        "<?php\nnamespace App\\Services;\nclass Mailer {}\nfunction Render() {}\n",
    )
    .expect("write php file");

    let symbols = build_php_local_symbol_index(&[path]);

    assert!(symbols.contains(r"app\services\mailer"));
    assert!(symbols.contains(r"app\services\render"));
    assert!(!symbols.contains(r"App\Services\Mailer"));
    assert!(!symbols.contains(r"App\Services\Render"));
}
