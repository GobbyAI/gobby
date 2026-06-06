use super::common::*;
use std::fs;

#[test]
fn rust_grouped_imports_register_named_bare_bindings() {
    let mut extracted = ExtractedImports::default();

    parse_import_statement(
        "rust",
        "use std::collections::{HashMap, HashSet as Set};",
        "src/lib.rs",
        &ImportResolutionContext::default(),
        &mut extracted,
    )
    .expect("parse Rust grouped import");

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
    )
    .expect("parse Rust glob import");

    assert!(extracted.bindings.bare.is_empty());
    assert!(extracted.bindings.member.is_empty());
}

#[test]
fn js_type_only_import_with_default_and_named_clause_registers_no_bindings() {
    let import_context = ImportResolutionContext {
        js_external_packages: HashSet::from(["react".to_string()]),
        ..Default::default()
    };
    let mut extracted = ExtractedImports::default();

    parse_import_statement(
        "javascript",
        r#"import type React, { type ComponentProps, Node } from "react";"#,
        "src/app.tsx",
        &import_context,
        &mut extracted,
    )
    .expect("parse JS type import");

    assert_eq!(
        extracted
            .imports
            .first()
            .map(|import| import.module_name.as_str()),
        Some("react")
    );
    assert!(extracted.bindings.bare.is_empty());
    assert!(extracted.bindings.member.is_empty());
}

#[test]
fn python_empty_module_imports_do_not_register_alias_bindings() {
    let mut extracted = ExtractedImports::default();

    parse_import_statement(
        "python",
        "import  as alias",
        "src/sample.py",
        &ImportResolutionContext::default(),
        &mut extracted,
    )
    .expect("parse malformed Python import");
    parse_import_statement(
        "python",
        "from  import thing as alias",
        "src/sample.py",
        &ImportResolutionContext::default(),
        &mut extracted,
    )
    .expect("parse malformed Python from import");

    assert!(extracted.imports.is_empty());
    assert!(extracted.bindings.bare.is_empty());
    assert!(extracted.bindings.member.is_empty());
}

#[test]
fn python_alias_imports_register_member_binding() {
    let mut extracted = ExtractedImports::default();

    parse_import_statement(
        "python",
        "import numpy as np",
        "src/sample.py",
        &ImportResolutionContext::default(),
        &mut extracted,
    )
    .expect("parse aliased Python import");

    assert_eq!(extracted.imports[0].module_name, "numpy");
    assert_eq!(
        extracted.bindings.member.get("np").map(String::as_str),
        Some("numpy")
    );
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
    )
    .expect("parse PHP grouped import");

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
    )
    .expect("parse PHP grouped function import");

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
    )
    .expect("parse Swift local import");

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
    )
    .expect("parse Swift external import");

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
    )
    .expect("parse PHP grouped const import");

    assert_eq!(
        extracted.bindings.member.get("V").map(String::as_str),
        Some(r"Vendor\Pkg\VALUE")
    );
}

#[test]
fn php_malformed_grouped_imports_do_not_register_literal_brace_paths() {
    let mut extracted = ExtractedImports::default();

    parse_import_statement(
        "php",
        r"use Vendor\Pkg\{Client, Helper;",
        "src/sample.php",
        &ImportResolutionContext::default(),
        &mut extracted,
    )
    .expect("parse malformed PHP grouped import");

    assert!(extracted.imports.is_empty());
    assert!(extracted.bindings.bare.is_empty());
    assert!(extracted.bindings.member.is_empty());
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
    )
    .expect("parse PHP wildcard import");

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
    )
    .expect("parse PHP path wildcard import");

    assert_eq!(
        extracted.bindings.bare_wildcard_modules,
        vec![r"Vendor".to_string()]
    );
    assert!(extracted.bindings.member.is_empty());
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
    )
    .expect("parse Go backtick import");

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
fn java_non_import_statement_is_ignored() {
    let mut extracted = ExtractedImports::default();

    parse_import_statement(
        "java",
        "package com.example;",
        "src/main/java/com/example/App.java",
        &ImportResolutionContext::default(),
        &mut extracted,
    )
    .expect("ignore Java non-import statement");

    assert!(extracted.imports.is_empty());
    assert!(extracted.bindings.member.is_empty());
}

#[test]
fn csharp_non_using_statement_is_ignored() {
    let mut extracted = ExtractedImports::default();

    parse_import_statement(
        "csharp",
        "namespace Example;",
        "Program.cs",
        &ImportResolutionContext::default(),
        &mut extracted,
    )
    .expect("ignore C# non-using statement");

    assert!(extracted.imports.is_empty());
    assert!(extracted.bindings.member.is_empty());
    assert!(extracted.bindings.external_roots.is_empty());
}

#[test]
fn non_import_statements_are_ignored_for_other_import_parsers() {
    let cases = [
        ("php", "namespace App;", "src/sample.php"),
        ("kotlin", "package com.example", "src/main/kotlin/App.kt"),
        (
            "swift",
            "let value = Foundation.Date()",
            "Sources/App/main.swift",
        ),
        ("dart", "library app;", "lib/main.dart"),
        ("elixir", "defmodule App do", "lib/app.ex"),
    ];

    for (language, text, path) in cases {
        let mut extracted = ExtractedImports::default();

        parse_import_statement(
            language,
            text,
            path,
            &ImportResolutionContext::default(),
            &mut extracted,
        )
        .expect("ignore non-import statement");

        assert!(
            extracted.imports.is_empty(),
            "{language} recorded imports for `{text}`"
        );
        assert!(
            extracted.bindings.member.is_empty(),
            "{language} recorded member bindings for `{text}`"
        );
        assert!(
            extracted.bindings.external_roots.is_empty(),
            "{language} recorded external roots for `{text}`"
        );
        assert!(
            extracted.bindings.bare_wildcard_modules.is_empty(),
            "{language} recorded wildcard modules for `{text}`"
        );
    }
}

#[test]
fn python_and_js_fallback_imports_are_marked_unparsed() {
    let mut extracted = ExtractedImports::default();
    parse_import_statement(
        "python",
        "include plugin_imports",
        "src/plugin.py",
        &ImportResolutionContext::default(),
        &mut extracted,
    )
    .expect("record python fallback import");

    assert_eq!(
        extracted.imports[0].module_name,
        "UNPARSED:include plugin_imports"
    );

    let mut extracted = ExtractedImports::default();
    parse_import_statement(
        "javascript",
        "sideEffectOnly();",
        "src/plugin.js",
        &ImportResolutionContext::default(),
        &mut extracted,
    )
    .expect("record JS fallback import");

    assert_eq!(
        extracted.imports[0].module_name,
        "UNPARSED:sideEffectOnly();"
    );
}

#[test]
fn unknown_language_fallback_rejects_empty_or_multiline_text() {
    for text in ["", "import one\nimport two"] {
        let mut extracted = ExtractedImports::default();
        let error = parse_import_statement(
            "unknown",
            text,
            "src/sample.unknown",
            &ImportResolutionContext::default(),
            &mut extracted,
        )
        .expect_err("invalid fallback text must fail");

        assert!(error.to_string().contains("unparsed import fallback"));
        assert!(extracted.imports.is_empty());
    }
}
