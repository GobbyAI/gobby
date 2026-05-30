use super::common::{parse_kotlin, parse_swift};

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
            .any(|import| import.module_name == "import kotlinx.coroutines.runBlocking")
    );
    assert!(
        parsed
            .calls
            .iter()
            .any(|call| call.callee_name == "runBlocking"
                && call.callee_target_kind.as_str() == "unresolved")
    );
}

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
fn leaves_swift_unqualified_and_member_calls_unresolved() {
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

    assert_eq!(parsed.calls.len(), 2);
    assert!(
        parsed
            .calls
            .iter()
            .all(|call| call.callee_target_kind.as_str() == "unresolved")
    );
}
