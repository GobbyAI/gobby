use super::common::{parse_csharp, parse_go, parse_java, parse_rust, parse_source};
use crate::models::ParseResult;

fn parsed_symbol_id(parsed: &ParseResult, file_path: &str, name: &str, kind: &str) -> String {
    parsed
        .symbols
        .iter()
        .find(|symbol| symbol.file_path == file_path && symbol.name == name && symbol.kind == kind)
        .unwrap_or_else(|| panic!("{file_path}:{kind}:{name} symbol"))
        .id
        .clone()
}

/// Assert a cross-file Rust local call was recorded as a pending `local_import`
/// with the original name and the right candidate module file. The canonical id
/// is resolved post-write against `code_symbols` (covered by the live
/// integration test), not at parse time.
///
/// A macro (rather than a function) so the assertions expand directly into each
/// test body.
macro_rules! assert_rust_local_import {
    ($parsed:expr, $callee_name:expr, $expected_file:expr) => {{
        let callee_name: &str = $callee_name;
        let call = $parsed
            .calls
            .iter()
            .find(|call| {
                call.callee_target_kind.as_str() == "local_import"
                    && call.callee_name == callee_name
            })
            .unwrap_or_else(|| panic!("missing local_import call for {callee_name}"));
        let candidates = call.local_import_candidate_files();
        assert!(
            candidates.iter().any(|file| file == $expected_file),
            "candidate files {candidates:?} did not contain {}",
            $expected_file
        );
    }};
}

#[test]
fn classifies_external_go_import_alias_selector_calls() {
    let parsed = parse_go(
        r#"
package main

import (
    "fmt"
    cli "github.com/acme/client"
    "gopkg.in/yaml.v3"
)

func run() {
    fmt.Println("hello")
    cli.Connect()
    yaml.Unmarshal(nil, nil)
}
"#,
        &[("go.mod", "module example.com/app\n")],
    );

    let fmt_call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "Println")
        .expect("fmt call");
    assert_eq!(fmt_call.callee_target_kind.as_str(), "external");
    assert_eq!(fmt_call.callee_external_module.as_deref(), Some("fmt"));

    let alias_call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "Connect")
        .expect("alias call");
    assert_eq!(alias_call.callee_target_kind.as_str(), "external");
    assert_eq!(
        alias_call.callee_external_module.as_deref(),
        Some("github.com/acme/client")
    );

    let yaml_call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "Unmarshal")
        .expect("yaml call");
    assert_eq!(yaml_call.callee_target_kind.as_str(), "external");
    assert_eq!(
        yaml_call.callee_external_module.as_deref(),
        Some("gopkg.in/yaml.v3")
    );
}

#[test]
fn resolves_self_module_go_selector_calls_to_local_package_files() {
    let parsed = parse_go(
        r#"
package main

import "example.com/app/pkg/tool"

func run() {
    tool.Run()
}
"#,
        &[
            ("go.mod", "module example.com/app\n"),
            ("pkg/tool/tool.go", "package tool\n\nfunc Run() {}\n"),
        ],
    );

    // The default package alias (last path segment `tool`) binds the selector
    // call `tool.Run()` to the package directory's Go files; the post-write DB
    // pass narrows it to the canonical `Run` symbol.
    assert_rust_local_import!(&parsed, "Run", "pkg/tool/tool.go");
}

#[test]
fn resolves_aliased_go_selector_calls_against_any_package_file() {
    let parsed = parse_go(
        r#"
package main

import svc "example.com/app/internal/service"

func run() {
    svc.Start()
}
"#,
        &[
            ("go.mod", "module example.com/app\n"),
            (
                "internal/service/service.go",
                "package service\n\ntype Server struct{}\n",
            ),
            (
                "internal/service/lifecycle.go",
                "package service\n\nfunc Start() {}\n",
            ),
        ],
    );

    let call = parsed
        .calls
        .iter()
        .find(|call| {
            call.callee_target_kind.as_str() == "local_import" && call.callee_name == "Start"
        })
        .expect("local_import call for Start");
    let candidates = call.local_import_candidate_files();
    // Package-granular: the import alias binds the whole package directory, so
    // the candidate set spans every Go file in it. The target `Start` lives in
    // lifecycle.go, not the eponymous service.go, yet still resolves.
    assert!(
        candidates.contains(&"internal/service/lifecycle.go".to_string()),
        "candidates {candidates:?} missing lifecycle.go"
    );
    assert!(
        candidates.contains(&"internal/service/service.go".to_string()),
        "candidates {candidates:?} missing service.go"
    );
}

#[test]
fn leaves_self_module_go_imports_without_package_files_unresolved() {
    // A self-module import whose package directory has no indexed Go files
    // produces no local binding, so the selector call degrades to unresolved
    // rather than a guessed (phantom) target.
    let parsed = parse_go(
        r#"
package main

import "example.com/app/pkg/tool"

func run() {
    tool.Run()
}
"#,
        &[("go.mod", "module example.com/app\n")],
    );

    let call = parsed.calls.first().expect("call");
    assert_eq!(call.callee_target_kind.as_str(), "unresolved");
}

#[test]
fn leaves_unimported_go_selector_calls_unresolved() {
    let parsed = parse_go(
        r#"
package main

func run(client Client) {
    client.Do()
}
"#,
        &[("go.mod", "module example.com/app\n")],
    );

    let call = parsed.calls.first().expect("call");
    assert_eq!(call.callee_target_kind.as_str(), "unresolved");
}

#[test]
fn classifies_external_rust_use_alias_and_path_calls() {
    let parsed = parse_rust(
        r#"
use serde_json::from_str as parse_json;
use std::fs;

fn run() {
    parse_json("{}");
    serde_json::from_str("{}");
    fs::read("Cargo.toml");
    std::fs::read("Cargo.toml");
}
"#,
        &[(
            "Cargo.toml",
            r#"[package]
name = "app"

[dependencies]
serde_json = { version = "1" }
"#,
        )],
    );

    parsed
        .calls
        .iter()
        .find(|call| {
            call.callee_name == "from_str"
                && call.callee_target_kind.as_str() == "external"
                && call.callee_external_module.as_deref() == Some("serde_json")
        })
        .expect("from_str call");

    let read_modules: Vec<_> = parsed
        .calls
        .iter()
        .filter(|call| call.callee_name == "read")
        .map(|call| call.callee_external_module.as_deref())
        .collect();
    assert_eq!(read_modules, vec![Some("std::fs"), Some("std::fs")]);
}

#[test]
fn records_rust_self_crate_import_and_leaves_glob_unresolved() {
    let parsed = parse_rust(
        r#"
use app::helper;
use serde_json::*;

fn run() {
    helper();
    from_str("{}");
}
"#,
        &[(
            "Cargo.toml",
            r#"[package]
name = "app"

[dependencies]
serde_json = "1"
"#,
        )],
    );

    assert_eq!(parsed.calls.len(), 2);
    // `use app::helper` names this crate's own crate-root item, so it is a local
    // import resolved against the crate-root files (lib.rs/main.rs) post-write.
    assert_rust_local_import!(&parsed, "helper", "src/lib.rs");
    // A glob import binds no specific name, so the bare `from_str` call stays
    // unresolved.
    let from_str = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "from_str")
        .expect("from_str call");
    assert_eq!(from_str.callee_target_kind.as_str(), "unresolved");
}

#[test]
fn records_rust_local_imported_and_module_qualified_calls() {
    let parsed = parse_rust(
        r#"
use crate::service::helper as run_helper;
use app::service::other;

fn run() {
    run_helper();
    other();
    crate::service::direct();
}
"#,
        &[
            (
                "Cargo.toml",
                r#"[package]
name = "app"
"#,
            ),
            (
                "src/service.rs",
                "pub fn helper() {}\npub fn other() {}\npub fn direct() {}\n",
            ),
        ],
    );

    // `use` alias (`run_helper` -> `helper`), self-crate path (`app::service`),
    // and a bare path-qualified call (`crate::service::direct`) all record the
    // original name and resolve to `src/service.rs`.
    for name in ["helper", "other", "direct"] {
        assert_rust_local_import!(&parsed, name, "src/service.rs");
    }
}

#[test]
fn records_rust_grouped_super_imports_as_local_import_calls() {
    let parsed = parse_source(
        "src/index/import_resolution/context.rs",
        r#"
use super::rust_local::{helper, other as run_other};

fn run() {
    helper();
    run_other();
}
"#,
        &[
            (
                "Cargo.toml",
                r#"[package]
name = "app"
"#,
            ),
            (
                "src/index/import_resolution/rust_local.rs",
                "pub(crate) fn helper() {}\npub(crate) fn other() {}\n",
            ),
        ],
    );

    // `super::` normalizes against the caller module tree to the sibling file.
    for name in ["helper", "other"] {
        assert_rust_local_import!(&parsed, name, "src/index/import_resolution/rust_local.rs");
    }
}

#[test]
fn resolves_rust_associated_same_file_calls_to_canonical_method() {
    let parsed = parse_rust(
        r#"
struct Parser;

impl Parser {
    fn new() -> Self {
        Parser
    }
}

fn run() {
    Parser::new();
}
"#,
        &[(
            "Cargo.toml",
            r#"[package]
name = "app"
"#,
        )],
    );

    let expected_id = parsed_symbol_id(&parsed, "src/main.rs", "new", "method");
    let call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "new")
        .expect("new call");
    assert_eq!(call.callee_target_kind.as_str(), "symbol");
    assert_eq!(call.callee_symbol_id.as_deref(), Some(expected_id.as_str()));
}

#[test]
fn rust_impl_blocks_do_not_emit_duplicate_type_symbols_and_parent_methods() {
    let parsed = parse_rust(
        r#"
struct Parser;

impl Parser {
    fn new() -> Self {
        Parser
    }
}

impl Parser {
    fn reset(&mut self) {}
}
"#,
        &[(
            "Cargo.toml",
            r#"[package]
name = "app"
"#,
        )],
    );

    let parser_symbols = parsed
        .symbols
        .iter()
        .filter(|symbol| {
            symbol.file_path == "src/main.rs"
                && symbol.name == "Parser"
                && (symbol.kind == "class" || symbol.kind == "type")
        })
        .collect::<Vec<_>>();
    assert_eq!(
        parser_symbols.len(),
        1,
        "Parser should have exactly one type symbol: {parser_symbols:?}"
    );
    let parser_id = parser_symbols[0].id.as_str();

    for method_name in ["new", "reset"] {
        let method = parsed
            .symbols
            .iter()
            .find(|symbol| symbol.name == method_name && symbol.kind == "method")
            .unwrap_or_else(|| panic!("missing {method_name} method"));
        assert_eq!(method.parent_symbol_id.as_deref(), Some(parser_id));
        assert_eq!(
            method.qualified_name,
            format!("Parser::{method_name}"),
            "{method_name} should be qualified under the canonical Parser symbol"
        );
    }

    assert!(
        parsed.symbols.iter().all(|symbol| {
            !symbol
                .signature
                .as_deref()
                .is_some_and(|signature| signature.starts_with("impl Parser"))
        }),
        "impl blocks must not be emitted as symbols: {:?}",
        parsed.symbols
    );
}

#[test]
fn classifies_rust_workspace_member_dependencies() {
    let parsed = parse_rust(
        r#"
use serde_json::from_str;

fn run() {
    from_str("{}");
}
"#,
        &[
            (
                "Cargo.toml",
                r#"[workspace]
members = ["crates/app"]
"#,
            ),
            (
                "crates/app/Cargo.toml",
                r#"[package]
name = "app"

[dependencies]
serde_json = "1"
"#,
            ),
        ],
    );

    let call = parsed.calls.first().expect("from_str call");
    assert_eq!(call.callee_target_kind.as_str(), "external");
    assert_eq!(call.callee_external_module.as_deref(), Some("serde_json"));
}

#[test]
fn leaves_rust_receiver_method_calls_unresolved() {
    let parsed = parse_rust(
        r#"
fn run(value: Parser) {
    value.parse();
}
"#,
        &[(
            "Cargo.toml",
            r#"[package]
name = "app"
"#,
        )],
    );

    let call = parsed.calls.first().expect("call");
    assert_eq!(call.callee_target_kind.as_str(), "unresolved");
}

#[test]
fn classifies_external_java_import_and_static_import_calls() {
    let parsed = parse_java(
        r#"
package app;

import java.util.Collections;
import static java.util.Objects.requireNonNull;

class Sample {
    void run() {
        Collections.emptyList();
        requireNonNull("x");
    }
}
"#,
        &[],
    );

    let class_call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "emptyList")
        .expect("class call");
    assert_eq!(class_call.callee_target_kind.as_str(), "external");
    assert_eq!(
        class_call.callee_external_module.as_deref(),
        Some("java.util.Collections")
    );

    let static_call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "requireNonNull")
        .expect("static call");
    assert_eq!(static_call.callee_target_kind.as_str(), "external");
    assert_eq!(
        static_call.callee_external_module.as_deref(),
        Some("java.util.Objects")
    );
}

#[test]
fn leaves_java_wildcard_and_instance_calls_unresolved() {
    let parsed = parse_java(
        r#"
package app;

import java.util.*;

class Sample {
    void run(java.util.List<String> list) {
        list.add("x");
        emptyList();
    }
}
"#,
        &[],
    );

    assert_eq!(parsed.calls.len(), 2);
    assert!(
        parsed
            .calls
            .iter()
            .all(|call| call.callee_target_kind.as_str() == "unresolved")
    );
}

#[test]
fn resolves_local_java_single_type_import_member_and_constructor_calls() {
    // A local single-type import binds the class alias for both a static member
    // call (`Helper.render()`) and a constructor call (`new Helper()`); each
    // becomes a `local_import` pointing at the declaring class file.
    let parsed = parse_java(
        r#"
package app;

import app.helpers.Helper;

class Sample {
    void run() {
        Helper.render();
        new Helper();
    }
}
"#,
        &[(
            "src/main/java/app/helpers/Helper.java",
            r#"
package app.helpers;

class Helper {
    static void render() {}
}
"#,
        )],
    );

    // Static member call resolves the method against the class file.
    assert_rust_local_import!(&parsed, "render", "src/main/java/app/helpers/Helper.java");
    // Constructor call resolves the class itself against the same file.
    assert_rust_local_import!(&parsed, "Helper", "src/main/java/app/helpers/Helper.java");
}

#[test]
fn resolves_local_java_static_imports() {
    // A local static import binds the bare member name to its declaring class
    // file so `square(..)` resolves to the static method symbol.
    let parsed = parse_java(
        r#"
package app;

import static app.util.Maths.square;

class Sample {
    void run() {
        square(3);
    }
}
"#,
        &[(
            "src/main/java/app/util/Maths.java",
            r#"
package app.util;

class Maths {
    static int square(int x) {
        return x * x;
    }
}
"#,
        )],
    );

    assert_rust_local_import!(&parsed, "square", "src/main/java/app/util/Maths.java");
}

#[test]
fn leaves_local_java_imports_without_class_files_unresolved() {
    // The simple name `Helper` is locally declared (in `app.other`), so the
    // import `app.missing.Helper` classifies as local — but no file declares
    // that fully-qualified class, so the call degrades to unresolved rather
    // than minting a false edge.
    let parsed = parse_java(
        r#"
package app;

import app.missing.Helper;

class Sample {
    void run() {
        Helper.render();
    }
}
"#,
        &[(
            "src/main/java/app/other/Helper.java",
            r#"
package app.other;

class Helper {}
"#,
        )],
    );

    let call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "render")
        .expect("render call");
    assert_eq!(call.callee_target_kind.as_str(), "unresolved");
}

#[test]
fn classifies_external_csharp_alias_static_and_qualified_calls() {
    let parsed = parse_csharp(
        r#"
using Json = Newtonsoft.Json.JsonConvert;
using static System.Math;
using System;

class Sample {
    void Run() {
        Json.SerializeObject(this);
        Sqrt(4);
        System.Console.WriteLine("x");
    }
}
"#,
        &[],
    );

    let alias_call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "SerializeObject")
        .expect("alias call");
    assert_eq!(alias_call.callee_target_kind.as_str(), "external");
    assert_eq!(
        alias_call.callee_external_module.as_deref(),
        Some("Newtonsoft.Json.JsonConvert")
    );

    let static_call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "Sqrt")
        .expect("static call");
    assert_eq!(static_call.callee_target_kind.as_str(), "external");
    assert_eq!(
        static_call.callee_external_module.as_deref(),
        Some("System.Math")
    );

    let qualified_call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "WriteLine")
        .expect("qualified call");
    assert_eq!(qualified_call.callee_target_kind.as_str(), "external");
    assert_eq!(
        qualified_call.callee_external_module.as_deref(),
        Some("System.Console")
    );
}

#[test]
fn classifies_external_csharp_global_alias_calls() {
    let parsed = parse_csharp(
        r#"
using Json = global::Newtonsoft.Json.JsonConvert;
using global::System;

class Sample {
    void Run() {
        Json.SerializeObject(this);
        global::System.Console.WriteLine("x");
    }
}
"#,
        &[],
    );

    let alias_call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "SerializeObject")
        .expect("alias call");
    assert_eq!(alias_call.callee_target_kind.as_str(), "external");
    assert_eq!(
        alias_call.callee_external_module.as_deref(),
        Some("Newtonsoft.Json.JsonConvert")
    );

    let qualified_call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "WriteLine")
        .expect("qualified call");
    assert_eq!(qualified_call.callee_target_kind.as_str(), "external");
    assert_eq!(
        qualified_call.callee_external_module.as_deref(),
        Some("System.Console")
    );
}

#[test]
fn resolves_local_csharp_namespace_and_fully_qualified_member_calls() {
    // A namespace import (`using App.Helpers;`) makes a simple-type member call
    // (`Tool.Render()`) resolvable, and a fully-qualified member call
    // (`App.Helpers.Tool.Render()`) resolves without any import. Both become a
    // `local_import` pointing at the declaring type's file.
    let parsed = parse_csharp(
        r#"
namespace App;

using App.Helpers;

class Sample {
    void Run() {
        Tool.Render();
        App.Helpers.Tool.Render();
    }
}
"#,
        &[(
            "src/Helpers/Tool.cs",
            r#"
namespace App.Helpers;

class Tool {
    public static void Render() {}
}
"#,
        )],
    );

    let resolved_render = parsed
        .calls
        .iter()
        .filter(|call| {
            call.callee_name == "Render" && call.callee_target_kind.as_str() == "local_import"
        })
        .count();
    assert_eq!(
        resolved_render, 2,
        "both the namespace-imported and fully-qualified calls should resolve"
    );
    assert_rust_local_import!(&parsed, "Render", "src/Helpers/Tool.cs");
}

#[test]
fn resolves_local_csharp_type_alias_member_calls() {
    // A local type alias (`using W = App.Helpers.Widget;`) binds the alias for a
    // member call (`W.Build()`); it resolves to the aliased type's file.
    let parsed = parse_csharp(
        r#"
namespace App;

using W = App.Helpers.Widget;

class Sample {
    void Run() {
        W.Build();
    }
}
"#,
        &[(
            "src/Helpers/Widget.cs",
            r#"
namespace App.Helpers;

class Widget {
    public static void Build() {}
}
"#,
        )],
    );

    assert_rust_local_import!(&parsed, "Build", "src/Helpers/Widget.cs");
}

#[test]
fn leaves_csharp_instance_and_unimported_namespace_calls_unresolved() {
    // An instance call (`client.Send()`) has no statically known type, and a
    // namespace import (`using App.Missing;`) that does not actually declare the
    // referenced type leaves `Tool.Render()` unresolved rather than minting a
    // false edge to the unrelated `App.Other.Tool`.
    let parsed = parse_csharp(
        r#"
namespace App;

using App.Missing;

class Sample {
    void Run(Client client) {
        client.Send();
        Tool.Render();
    }
}
"#,
        &[(
            "src/Other/Tool.cs",
            r#"
namespace App.Other;

class Tool {
    public static void Render() {}
}
"#,
        )],
    );

    assert_eq!(parsed.calls.len(), 2);
    assert!(
        parsed
            .calls
            .iter()
            .all(|call| call.callee_target_kind.as_str() == "unresolved")
    );
}

/// #791 no-phantom regression (services-free half), Rust arm: an attributed,
/// doc-commented `pub fn` is extracted as a top-level symbol, and a `crate::`
/// use of it from a sibling module is recorded as a resolvable `local_import`
/// pointing at the defining file. Post-#790 the resolver matches the imported
/// name against the extracted symbol, so the `#[inline]`/doc-comment byte offset
/// shift cannot desync the target id.
#[test]
fn attributed_rust_definitions_extract_and_resolve_as_local_imports() {
    let defs =
        "/// Doc-commented, attributed cross-file target.\n#[inline]\npub fn rust_target() {}\n";
    let cargo = "[package]\nname = \"app\"\n";

    let target = parse_source("src/service.rs", defs, &[("Cargo.toml", cargo)]);
    let symbol = target
        .symbols
        .iter()
        .find(|symbol| symbol.name == "rust_target")
        .expect("rust_target symbol");
    assert_eq!(symbol.kind.as_str(), "function");
    assert!(
        symbol.parent_symbol_id.is_none(),
        "rust_target should be a top-level symbol"
    );

    let caller = parse_source(
        "src/lib.rs",
        "pub mod service;\nuse crate::service::rust_target;\nfn run() {\n    rust_target();\n}\n",
        &[("Cargo.toml", cargo), ("src/service.rs", defs)],
    );
    assert_rust_local_import!(&caller, "rust_target", "src/service.rs");
}
