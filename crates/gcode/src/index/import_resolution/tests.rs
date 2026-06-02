use std::collections::HashMap;
use std::fs;

use tempfile::TempDir;

use super::context::{
    load_dart_external_packages, load_elixir_dependency_names, load_rust_external_crates,
    load_rust_self_crate_name,
};
use super::helpers::{extract_quoted_string, go_default_package_alias, split_top_level};
use super::predicates::{
    bundled_elixir_dependency_roots, bundled_ruby_require_roots, csharp_declared_types,
    elixir_dependency_roots, is_external_java_class, is_external_js_module, java_declared_types,
    ruby_require_root,
};
use super::*;

#[test]
fn loads_rust_inline_table_dependency_names() {
    let tempdir = TempDir::new().expect("tempdir");
    fs::write(
        tempdir.path().join("Cargo.toml"),
        r#"
[package]
name = "app"

[dependencies]
serde = { version = "1.0" }
"tokio-util" = { version = "0.7", features = ["codec"] }
"#,
    )
    .expect("cargo toml");

    let crates = load_rust_external_crates(tempdir.path());

    assert!(crates.contains("serde"));
    assert!(crates.contains("tokio_util"));
}

#[test]
fn loads_rust_dependency_names_from_real_toml_tables() {
    let tempdir = TempDir::new().expect("tempdir");
    fs::write(
        tempdir.path().join("Cargo.toml"),
        r#"
[package]
name = "app"

[dependencies]
serde = "1" # keep comment parsing delegated to TOML

[dev-dependencies]
pretty-assertions = "1"

[build-dependencies]
bindgen = "0.69"

[target.'cfg(unix)'.dependencies]
nix = "0.27"

[target.x86_64-pc-windows-msvc.dev-dependencies]
windows-sys = "0.52"

[target.'cfg(target_os = "linux")'.build-dependencies]
cc = "1"
"#,
    )
    .expect("cargo toml");

    let crates = load_rust_external_crates(tempdir.path());

    for name in [
        "serde",
        "pretty_assertions",
        "bindgen",
        "nix",
        "windows_sys",
        "cc",
    ] {
        assert!(crates.contains(name), "missing {name}");
    }
}

#[test]
fn ignores_rust_non_dependency_toml_tables() {
    let tempdir = TempDir::new().expect("tempdir");
    fs::write(
        tempdir.path().join("Cargo.toml"),
        r#"
[package]
name = "app"

[workspace.dependencies]
workspace-only = "1"

[package.metadata.dependencies]
metadata-only = "1"

[features]
serde = []
"#,
    )
    .expect("cargo toml");

    let crates = load_rust_external_crates(tempdir.path());

    assert!(!crates.contains("workspace_only"));
    assert!(!crates.contains("metadata_only"));
    assert!(!crates.contains("serde"));
}

#[test]
fn normalizes_rust_package_name_from_cargo_toml() {
    let tempdir = TempDir::new().expect("tempdir");
    fs::write(
        tempdir.path().join("Cargo.toml"),
        r#"
[package]
name = "my-crate"
"#,
    )
    .expect("cargo toml");

    assert_eq!(
        load_rust_self_crate_name(tempdir.path()).as_deref(),
        Some("my_crate")
    );
}

#[test]
fn missing_dart_pubspec_loads_no_external_packages() {
    let tempdir = TempDir::new().expect("tempdir");

    let packages = load_dart_external_packages(tempdir.path());

    assert!(packages.is_empty());
}

#[test]
fn bundled_import_root_json_parses() {
    assert_eq!(
        bundled_ruby_require_roots().get("json").map(String::as_str),
        Some("JSON")
    );
    assert!(
        bundled_elixir_dependency_roots()
            .get("jason")
            .is_some_and(|roots| roots.iter().any(|root| root == "Jason"))
    );
}

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
fn rust_grouped_imports_register_named_bare_bindings() {
    let mut extracted = ExtractedImports::default();

    parse_import_statement(
        "rust",
        "use std::collections::{HashMap, HashSet as Set};",
        "src/lib.rs",
        &ImportResolutionContext::default(),
        &mut extracted,
    );

    let hash_map = extracted
        .bindings
        .bare
        .get("HashMap")
        .expect("HashMap binding");
    assert_eq!(hash_map.module, "std::collections");
    assert_eq!(hash_map.callee_name, "HashMap");
    let set = extracted.bindings.bare.get("Set").expect("Set binding");
    assert_eq!(set.module, "std::collections");
    assert_eq!(set.callee_name, "HashSet");
    assert_eq!(
        extracted.bindings.member.get("Set").map(String::as_str),
        Some("std::collections::HashSet")
    );
    assert!(extracted.bindings.external_roots.contains_key("std"));
}

#[test]
fn rust_glob_imports_do_not_register_individual_bare_bindings() {
    let mut extracted = ExtractedImports::default();

    parse_import_statement(
        "rust",
        "use std::collections::*;",
        "src/lib.rs",
        &ImportResolutionContext::default(),
        &mut extracted,
    );

    assert!(extracted.bindings.bare.is_empty());
    assert!(extracted.bindings.member.is_empty());
}

#[test]
fn php_grouped_imports_register_concrete_member_bindings() {
    let mut extracted = ExtractedImports::default();

    parse_import_statement(
        "php",
        r"use Vendor\Pkg\{Client, Helper as H};",
        "src/sample.php",
        &ImportResolutionContext::default(),
        &mut extracted,
    );

    assert!(
        extracted
            .imports
            .iter()
            .any(|import| import.module_name == r"Vendor\Pkg\Client")
    );
    assert!(
        extracted
            .imports
            .iter()
            .any(|import| import.module_name == r"Vendor\Pkg\Helper")
    );
    assert_eq!(
        extracted.bindings.member.get("Client").map(String::as_str),
        Some(r"Vendor\Pkg\Client")
    );
    assert_eq!(
        extracted.bindings.member.get("H").map(String::as_str),
        Some(r"Vendor\Pkg\Helper")
    );
}

#[test]
fn php_grouped_function_imports_register_concrete_bare_bindings() {
    let mut extracted = ExtractedImports::default();

    parse_import_statement(
        "php",
        r"use function Vendor\Pkg\{work, helper as do_help};",
        "src/sample.php",
        &ImportResolutionContext::default(),
        &mut extracted,
    );

    assert!(
        extracted
            .imports
            .iter()
            .any(|import| import.module_name == r"Vendor\Pkg\work")
    );
    let work = extracted
        .bindings
        .bare
        .get("work")
        .expect("function binding");
    assert_eq!(work.module, r"Vendor\Pkg");
    assert_eq!(work.callee_name, "work");
    let helper = extracted
        .bindings
        .bare
        .get("do_help")
        .expect("aliased function binding");
    assert_eq!(helper.module, r"Vendor\Pkg");
    assert_eq!(helper.callee_name, "helper");
}

#[test]
fn swift_imports_do_not_mark_local_modules_external() {
    let tempdir = TempDir::new().expect("tempdir");
    let local_file = tempdir.path().join("Sources/AppCore/Thing.swift");
    fs::create_dir_all(local_file.parent().expect("source parent")).expect("create source dir");
    fs::write(&local_file, "import Foundation\n").expect("write swift source");
    let context = build_import_resolution_context(tempdir.path(), &[local_file]);
    let mut extracted = ExtractedImports::default();

    parse_import_statement(
        "swift",
        "import AppCore",
        "Sources/AppCore/Thing.swift",
        &context,
        &mut extracted,
    );

    assert!(!extracted.bindings.external_roots.contains_key("AppCore"));
}

#[test]
fn swift_imports_still_mark_unknown_modules_external() {
    let context = ImportResolutionContext::default();
    let mut extracted = ExtractedImports::default();

    parse_import_statement(
        "swift",
        "import Foundation",
        "Sources/AppCore/Thing.swift",
        &context,
        &mut extracted,
    );

    assert!(extracted.bindings.external_roots.contains_key("Foundation"));
}

#[test]
fn php_grouped_const_imports_preserve_aliases() {
    let mut extracted = ExtractedImports::default();

    parse_import_statement(
        "php",
        r"use const Vendor\Pkg\{VALUE as V};",
        "src/sample.php",
        &ImportResolutionContext::default(),
        &mut extracted,
    );

    assert_eq!(
        extracted.bindings.member.get("V").map(String::as_str),
        Some(r"Vendor\Pkg\VALUE")
    );
}

#[test]
fn php_wildcard_imports_register_external_module_prefixes() {
    let mut extracted = ExtractedImports::default();

    parse_import_statement(
        "php",
        r"use Vendor\Pkg\*;",
        "src/sample.php",
        &ImportResolutionContext::default(),
        &mut extracted,
    );

    assert_eq!(
        extracted
            .bindings
            .bare_wildcard_modules
            .iter()
            .map(String::as_str)
            .collect::<Vec<_>>(),
        vec![r"Vendor\Pkg"]
    );
}

#[test]
fn php_path_segment_wildcards_do_not_register_literal_member_bindings() {
    let mut extracted = ExtractedImports::default();

    parse_import_statement(
        "php",
        r"use Vendor\*\Helper;",
        "src/sample.php",
        &ImportResolutionContext::default(),
        &mut extracted,
    );

    assert_eq!(
        extracted.bindings.bare_wildcard_modules,
        vec![r"Vendor".to_string()]
    );
    assert!(extracted.bindings.member.is_empty());
}

#[test]
fn loads_elixir_mix_lock_first_quoted_dependency_per_line() {
    let tempdir = TempDir::new().expect("tempdir");
    fs::write(
        tempdir.path().join("mix.lock"),
        r#"%{
  "jason": {:hex, :jason, "1.4.4", "checksum", [:mix], [], "hexpm", "repo"},
  "httpoison": {:hex, :httpoison, "2.2.1", "checksum", [:mix], [], "hexpm", "repo"}
}"#,
    )
    .expect("mix.lock");

    let deps = load_elixir_dependency_names(tempdir.path());

    assert!(deps.contains("jason"));
    assert!(deps.contains("httpoison"));
    assert!(!deps.contains("1"));
    assert!(!deps.contains("hexpm"));
}

#[test]
fn loads_elixir_mix_dependencies_with_whole_file_regex() {
    let tempdir = TempDir::new().expect("tempdir");
    fs::write(
        tempdir.path().join("mix.exs"),
        r#"
defp deps do
  [
    {
      :jason,
      "~> 1.4"
    },
    {:plug_cowboy,
     "~> 2.7"}
  ]
end
"#,
    )
    .expect("mix.exs");

    let deps = load_elixir_dependency_names(tempdir.path());

    assert!(deps.contains("jason"));
    assert!(deps.contains("plug_cowboy"));
}

#[test]
fn bundled_import_root_data_loads_known_mappings() {
    assert_eq!(ruby_require_root("json"), Some("JSON"));
    assert_eq!(ruby_require_root("unknown_gem"), None);

    let roots = elixir_dependency_roots("jason").expect("jason roots");
    assert_eq!(roots.first().map(String::as_str), Some("Jason"));
    assert_eq!(roots.len(), 1);
    assert!(elixir_dependency_roots("unknown_dep").is_none());
}

#[test]
fn runtime_import_root_overrides_take_precedence() {
    let tempdir = TempDir::new().expect("tempdir");
    fs::write(
        tempdir.path().join("mix.exs"),
        r#"
defp deps do
  [
    {:jason, "~> 1.4"}
  ]
end
"#,
    )
    .expect("mix.exs");

    let context = build_import_resolution_context_with_overrides(
        tempdir.path(),
        &[],
        HashMap::from([("json".to_string(), "RuntimeJSON".to_string())]),
        HashMap::from([
            ("Jason".to_string(), "RuntimeJason".to_string()),
            ("RuntimeOnly".to_string(), "RuntimeOnly".to_string()),
        ]),
    );

    let mut extracted = ExtractedImports::default();
    parse_import_statement(
        "ruby",
        r#"require "json""#,
        "app.rb",
        &context,
        &mut extracted,
    );
    assert!(
        extracted
            .bindings
            .external_roots
            .contains_key("RuntimeJSON")
    );
    assert!(!extracted.bindings.external_roots.contains_key("JSON"));

    let mut bindings = ImportBindings::default();
    seed_import_bindings("elixir", &context, &mut bindings);
    assert_eq!(
        bindings
            .external_roots
            .get("Jason")
            .map(|binding| binding.module.as_str()),
        Some("RuntimeJason")
    );
    assert_eq!(
        bindings
            .external_roots
            .get("RuntimeOnly")
            .map(|binding| binding.module.as_str()),
        Some("RuntimeOnly")
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
fn js_builtin_submodules_are_external_without_package_fallback() {
    let context = ImportResolutionContext::default();

    assert!(is_external_js_module("fs/promises", &context));
    assert!(is_external_js_module("stream/web", &context));
    assert!(!is_external_js_module("fs-extra/promises", &context));
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

#[test]
fn go_backtick_imports_register_external_bindings() {
    let import_context = ImportResolutionContext {
        go_module_path: Some("example.com/local".to_string()),
        ..Default::default()
    };
    let mut extracted = ExtractedImports::default();

    parse_import_statement(
        "go",
        "import api `github.com/acme/api-client`",
        "main.go",
        &import_context,
        &mut extracted,
    );

    assert_eq!(
        extracted
            .imports
            .first()
            .map(|import| import.module_name.as_str()),
        Some("github.com/acme/api-client")
    );
    assert_eq!(
        extracted.bindings.member.get("api").map(String::as_str),
        Some("github.com/acme/api-client")
    );
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
        .insert(r"App\Services\Mailer".to_string());

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
        .insert(r"App\Helpers\render".to_string());

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
