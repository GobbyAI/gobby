use super::common::{parse_kotlin, parse_swift};

/// Asserts a `local_import` call for `$callee_name` exists and its candidate
/// files include `$expected_file`. The post-write DB pass narrows these
/// candidates to the canonical symbol id.
macro_rules! assert_kotlin_local_import {
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

/// Asserts a `local_import` call for `$callee_name` exists and its candidate
/// files (every file in the caller's Swift module) include `$expected_file`.
/// The post-write DB pass narrows these candidates to the canonical symbol id.
macro_rules! assert_swift_local_import {
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
fn extracts_kotlin_symbols_imports_and_calls_without_external_classification() {
    let parsed = parse_kotlin(
        r#"
package app

import kotlinx.coroutines.runBlocking

class Runner {
    fun run() {
        runBlocking()
        println("hello")
    }
}
"#,
        &[],
    );

    assert!(
        parsed
            .symbols
            .iter()
            .any(|symbol| symbol.name == "Runner" && symbol.kind == "class")
    );
    assert!(
        parsed
            .symbols
            .iter()
            .any(|symbol| symbol.name == "run" && symbol.kind == "method")
    );
    assert!(
        parsed
            .imports
            .iter()
            .any(|import| import.module_name == "kotlinx.coroutines.runBlocking")
    );
    assert!(
        parsed
            .calls
            .iter()
            .any(|call| call.callee_name == "runBlocking"
                && call.callee_target_kind.as_str() == "unresolved")
    );
}

#[test]
fn resolves_local_kotlin_top_level_function_and_class_member_calls() {
    // A local import binds the imported name for a bare top-level function call
    // (`render()`), a class member call (`Widget.build()`), and a constructor
    // call (`Widget()`); each becomes a `local_import` pointing at the declaring
    // package's file(s).
    let parsed = parse_kotlin(
        r#"
package app

import app.helpers.render
import app.widgets.Widget

fun run() {
    render()
    Widget.build()
    Widget()
}
"#,
        &[
            (
                "src/main/kotlin/helpers/Helpers.kt",
                "package app.helpers\n\nfun render() {}\n",
            ),
            (
                "src/main/kotlin/widgets/Widget.kt",
                "package app.widgets\n\nclass Widget {\n    companion object {\n        fun build() {}\n    }\n}\n",
            ),
        ],
    );

    // Top-level function call resolves the imported function in its package file.
    assert_kotlin_local_import!(&parsed, "render", "src/main/kotlin/helpers/Helpers.kt");
    // Member call resolves the method against the class's package file.
    assert_kotlin_local_import!(&parsed, "build", "src/main/kotlin/widgets/Widget.kt");
    // Constructor call resolves the class itself against the same file.
    assert_kotlin_local_import!(&parsed, "Widget", "src/main/kotlin/widgets/Widget.kt");
}

#[test]
fn resolves_aliased_local_kotlin_imports() {
    // `import X as Y` binds the alias `Y` but records the original imported name
    // `X` as the call target, so the post-write pass resolves the real symbol.
    let parsed = parse_kotlin(
        r#"
package app

import app.widgets.Widget as W
import app.helpers.render as draw

fun run() {
    W.build()
    draw()
    W()
}
"#,
        &[
            (
                "src/main/kotlin/helpers/Helpers.kt",
                "package app.helpers\n\nfun render() {}\n",
            ),
            (
                "src/main/kotlin/widgets/Widget.kt",
                "package app.widgets\n\nclass Widget {\n    companion object {\n        fun build() {}\n    }\n}\n",
            ),
        ],
    );

    // Member call through the type alias resolves the method.
    assert_kotlin_local_import!(&parsed, "build", "src/main/kotlin/widgets/Widget.kt");
    // Bare call through the function alias records the original name `render`.
    assert_kotlin_local_import!(&parsed, "render", "src/main/kotlin/helpers/Helpers.kt");
    // Constructor call through the type alias records the original class name.
    assert_kotlin_local_import!(&parsed, "Widget", "src/main/kotlin/widgets/Widget.kt");
}

#[test]
fn leaves_external_and_unimported_kotlin_calls_unresolved() {
    // An external bare call stays unresolved (no false external bare binding),
    // and a member call on a type that exists locally but was never imported
    // must not resolve — no candidate-file binding exists for it.
    let parsed = parse_kotlin(
        r#"
package app

import kotlinx.coroutines.runBlocking

fun run() {
    runBlocking()
    Widget.build()
}
"#,
        &[(
            "src/main/kotlin/widgets/Widget.kt",
            "package app.widgets\n\nclass Widget {\n    companion object {\n        fun build() {}\n    }\n}\n",
        )],
    );

    assert!(
        !parsed
            .calls
            .iter()
            .any(|call| call.callee_target_kind.as_str() == "local_import"),
        "no call should resolve to a local import: {:?}",
        parsed.calls
    );
    assert!(
        parsed
            .calls
            .iter()
            .all(|call| call.callee_target_kind.as_str() != "local_import"),
        "unimported member and external bare calls must not become local imports",
    );
}

#[test]
fn classifies_external_swift_module_qualified_calls() {
    let parsed = parse_swift(
        r#"
import Foundation

func run() {
    Foundation.Date()
}
"#,
        &[],
    );

    let call = parsed.calls.first().expect("call");
    assert_eq!(call.callee_target_kind.as_str(), "external");
    assert_eq!(call.callee_name, "Date");
    assert_eq!(call.callee_external_module.as_deref(), Some("Foundation"));
}

#[test]
fn classifies_external_swift_scoped_import_module_qualified_calls() {
    let parsed = parse_swift(
        r#"
import struct Foundation.Date

func run() {
    Foundation.Date()
}
"#,
        &[],
    );

    let call = parsed.calls.first().expect("call");
    assert_eq!(call.callee_target_kind.as_str(), "external");
    assert_eq!(call.callee_name, "Date");
    assert_eq!(call.callee_external_module.as_deref(), Some("Foundation"));
}

#[test]
fn leaves_swift_member_calls_unresolved_and_routes_bare_calls_through_the_db() {
    let parsed = parse_swift(
        r#"
import Foundation

func run(date: Date) {
    Date()
    date.formatted()
}
"#,
        &[],
    );

    // A member call on a value has a dynamic receiver — it stays unresolved, so
    // no false edge is produced.
    let member = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "formatted")
        .expect("member call");
    assert_eq!(member.callee_target_kind.as_str(), "unresolved");

    // A bare call carries the caller's own module files as candidates and is
    // deferred to the post-write DB pass, which degrades it to unresolved when
    // (as here) no module file declares the name — never a phantom edge.
    assert_swift_local_import!(&parsed, "Date", "Sources/App/main.swift");
}

#[test]
fn resolves_local_swift_module_function_and_type_initializer_calls() {
    let parsed = parse_swift(
        r#"
func run() {
    greet()
    Widget()
}
"#,
        &[
            ("Sources/App/Greeter.swift", "func greet() {}\n"),
            ("Sources/App/Widget.swift", "struct Widget {}\n"),
        ],
    );

    // Swift has whole-module scope: a bare free-function call and a bare type
    // initializer call both resolve against every file sharing the caller's
    // module, with no `import` statement. The post-write DB pass narrows each
    // candidate set to the canonical function/type symbol id.
    assert_swift_local_import!(&parsed, "greet", "Sources/App/Greeter.swift");
    assert_swift_local_import!(&parsed, "Widget", "Sources/App/Widget.swift");
}
