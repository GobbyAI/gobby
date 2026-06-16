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
fn php_symbol_files_normalize_declared_names_to_lowercase_and_track_files() {
    let tempdir = TempDir::new().expect("tempdir");
    let path = tempdir.path().join("Mailer.php");
    fs::write(
        &path,
        "<?php\nnamespace App\\Services;\nclass Mailer {}\nfunction Render() {}\n",
    )
    .expect("write php file");

    let symbol_files = build_php_symbol_files(tempdir.path(), &[path]);

    assert!(symbol_files.contains_key(r"app\services\mailer"));
    assert!(symbol_files.contains_key(r"app\services\render"));
    assert!(symbol_files.contains_key("mailer"));
    assert!(!symbol_files.contains_key(r"App\Services\Mailer"));
    assert!(!symbol_files.contains_key(r"App\Services\Render"));
    assert_eq!(
        symbol_files.get(r"app\services\mailer").map(Vec::as_slice),
        Some(["Mailer.php".to_string()].as_slice())
    );
}

#[test]
fn swift_module_files_group_files_by_module_and_track_paths() {
    let tempdir = TempDir::new().expect("tempdir");
    let widget = tempdir.path().join("Sources/AppCore/Widget.swift");
    let main = tempdir.path().join("Sources/AppCore/main.swift");
    let other = tempdir.path().join("Sources/Helpers/Util.swift");
    for path in [&widget, &main, &other] {
        fs::create_dir_all(path.parent().expect("parent")).expect("mkdir");
        fs::write(path, "// swift\n").expect("write swift file");
    }

    let module_files = build_swift_module_files(
        tempdir.path(),
        &[widget.clone(), main.clone(), other.clone()],
    );

    // Files under `Sources/<Module>/` are grouped by `<Module>`, sorted and
    // deduped, with no cross-module bleed.
    assert_eq!(
        module_files.get("AppCore").map(Vec::as_slice),
        Some(
            [
                "Sources/AppCore/Widget.swift".to_string(),
                "Sources/AppCore/main.swift".to_string(),
            ]
            .as_slice()
        )
    );
    assert_eq!(
        module_files.get("Helpers").map(Vec::as_slice),
        Some(["Sources/Helpers/Util.swift".to_string()].as_slice())
    );
}

#[test]
fn elixir_module_files_map_full_module_names_to_declaring_files() {
    let tempdir = TempDir::new().expect("tempdir");
    let helper = tempdir.path().join("lib/app/helper.ex");
    let bundle = tempdir.path().join("lib/app/bundle.ex");
    let script = tempdir.path().join("script.exs");
    for path in [&helper, &bundle, &script] {
        fs::create_dir_all(path.parent().expect("parent")).expect("mkdir");
    }
    // A module's file path need not match its name, and one file may declare
    // several modules — both are exercised here.
    fs::write(&helper, "defmodule App.Helper do\nend\n").expect("write helper");
    fs::write(
        &bundle,
        "defmodule App.One do\nend\n\ndefmodule App.Two do\nend\n",
    )
    .expect("write bundle");
    fs::write(&script, "defmodule App.Script do\nend\n").expect("write script");

    let module_files = build_elixir_local_module_files(
        tempdir.path(),
        &[helper.clone(), bundle.clone(), script.clone()],
    );

    assert_eq!(
        module_files.get("App.Helper").map(Vec::as_slice),
        Some(["lib/app/helper.ex".to_string()].as_slice())
    );
    assert_eq!(
        module_files.get("App.One").map(Vec::as_slice),
        Some(["lib/app/bundle.ex".to_string()].as_slice())
    );
    assert_eq!(
        module_files.get("App.Two").map(Vec::as_slice),
        Some(["lib/app/bundle.ex".to_string()].as_slice())
    );
    // `.exs` scripts declare modules too.
    assert_eq!(
        module_files.get("App.Script").map(Vec::as_slice),
        Some(["script.exs".to_string()].as_slice())
    );
}

#[test]
fn dart_local_import_target_resolves_self_package_and_relative_uris() {
    // `package:<self>/…` maps into the package's `lib/` source root.
    assert_eq!(
        dart_local_import_target(
            "package:app/widgets/button.dart",
            "lib/main.dart",
            Some("app")
        ),
        Some("lib/widgets/button.dart".to_string())
    );
    // A relative URI resolves against the importing file's directory...
    assert_eq!(
        dart_local_import_target("helper.dart", "lib/src/main.dart", Some("app")),
        Some("lib/src/helper.dart".to_string())
    );
    // ...collapsing `..` segments that walk up out of it.
    assert_eq!(
        dart_local_import_target("../util.dart", "lib/src/main.dart", Some("app")),
        Some("lib/util.dart".to_string())
    );

    // `dart:` SDK imports and external `package:` dependencies are not local
    // project files.
    assert_eq!(
        dart_local_import_target("dart:async", "lib/main.dart", Some("app")),
        None
    );
    assert_eq!(
        dart_local_import_target("package:http/http.dart", "lib/main.dart", Some("app")),
        None
    );
    // Without a known self package, a `package:` URI cannot be resolved to a path.
    assert_eq!(
        dart_local_import_target("package:app/x.dart", "lib/main.dart", None),
        None
    );
}
