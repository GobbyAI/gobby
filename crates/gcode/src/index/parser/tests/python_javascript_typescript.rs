use super::common::{parse_javascript, parse_python, parse_source, parse_tsx, parse_typescript};

#[test]
fn classifies_external_python_from_import_calls() {
    let parsed = parse_python(
        r#"
from requests import get as fetch

def run():
    fetch()
"#,
        &[],
    );

    let call = parsed.calls.first().expect("call");
    assert_eq!(call.callee_target_kind.as_str(), "external");
    assert_eq!(call.callee_name, "get");
    assert_eq!(call.callee_external_module.as_deref(), Some("requests"));
}

#[test]
fn leaves_local_python_imports_unresolved() {
    let parsed = parse_source(
        "pkg/main.py",
        r#"
from pkg.utils import helper

def run():
    helper()
"#,
        &[("pkg/utils.py", "def helper():\n    pass\n")],
    );

    let call = parsed.calls.first().expect("call");
    assert_eq!(call.callee_target_kind.as_str(), "unresolved");
    assert!(call.callee_external_module.is_none());
}

#[test]
fn classifies_external_javascript_named_import_calls() {
    let parsed = parse_javascript(
        r#"
import { useState } from "react";

function run() {
  useState();
}
"#,
        &[(
            "package.json",
            r#"{"name":"app","dependencies":{"react":"^18.0.0"}}"#,
        )],
    );

    let call = parsed.calls.first().expect("call");
    assert_eq!(call.callee_target_kind.as_str(), "external");
    assert_eq!(call.callee_name, "useState");
    assert_eq!(call.callee_external_module.as_deref(), Some("react"));
}

#[test]
fn leaves_external_bare_calls_shadowed_by_parameters_unresolved() {
    let parsed = parse_javascript(
        r#"
import { useState } from "react";

function run(useState) {
  useState();
}
"#,
        &[(
            "package.json",
            r#"{"name":"app","dependencies":{"react":"^18.0.0"}}"#,
        )],
    );

    let call = parsed.calls.first().expect("call");
    assert_eq!(call.callee_target_kind.as_str(), "unresolved");
    assert!(call.callee_external_module.is_none());
}

#[test]
fn classifies_external_javascript_namespace_member_calls() {
    let parsed = parse_javascript(
        r#"
import * as React from "react";

function run() {
  React.useState();
}
"#,
        &[(
            "package.json",
            r#"{"name":"app","dependencies":{"react":"^18.0.0"}}"#,
        )],
    );

    let call = parsed.calls.first().expect("call");
    assert_eq!(call.callee_target_kind.as_str(), "external");
    assert_eq!(call.callee_name, "useState");
    assert_eq!(call.callee_external_module.as_deref(), Some("react"));
}

#[test]
fn leaves_relative_javascript_imports_unresolved() {
    let parsed = parse_javascript(
        r#"
import { helper } from "./utils";

function run() {
  helper();
}
"#,
        &[
            (
                "package.json",
                r#"{"name":"app","dependencies":{"react":"^18.0.0"}}"#,
            ),
            ("src/utils.js", "export function helper() {}\n"),
        ],
    );

    let call = parsed.calls.first().expect("call");
    assert_eq!(call.callee_target_kind.as_str(), "unresolved");
}

#[test]
fn classifies_external_typescript_default_member_calls() {
    let parsed = parse_typescript(
        r#"
import React from "react";

function run() {
  React.useState();
}
"#,
        &[(
            "package.json",
            r#"{"name":"app","dependencies":{"react":"^18.0.0"}}"#,
        )],
    );

    let call = parsed.calls.first().expect("call");
    assert_eq!(call.callee_target_kind.as_str(), "external");
    assert_eq!(call.callee_name, "useState");
    assert_eq!(call.callee_external_module.as_deref(), Some("react"));
}

#[test]
fn indexes_tsx_react_component_declarations() {
    let parsed = parse_tsx(
        r#"
export function ChatPage() {
  return <main />;
}

export default function App() {
  return <ChatPage />;
}

function LocalComponent() {
  return <section />;
}

const ArrowComponent = () => <aside />;
"#,
        &[],
    );

    let symbols: Vec<_> = parsed
        .symbols
        .iter()
        .map(|symbol| (symbol.name.as_str(), symbol.kind.as_str()))
        .collect();
    for expected in [
        ("ChatPage", "function"),
        ("App", "function"),
        ("LocalComponent", "function"),
        ("ArrowComponent", "function"),
    ] {
        assert!(
            symbols.contains(&expected),
            "missing {expected:?}; symbols: {symbols:?}"
        );
    }
}

#[test]
fn leaves_external_qualified_roots_shadowed_by_locals_unresolved() {
    let parsed = parse_typescript(
        r#"
import React from "react";

function run() {
  const React = makeLocalReact();
  React.useState();
}
"#,
        &[(
            "package.json",
            r#"{"name":"app","dependencies":{"react":"^18.0.0"}}"#,
        )],
    );

    let call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "useState")
        .expect("call");
    assert_eq!(call.callee_target_kind.as_str(), "unresolved");
    assert!(call.callee_external_module.is_none());
}

#[test]
fn leaves_unlisted_javascript_package_aliases_unresolved() {
    let parsed = parse_javascript(
        r#"
import { helper } from "@/utils";

function run() {
  helper();
}
"#,
        &[(
            "package.json",
            r#"{"name":"app","dependencies":{"react":"^18.0.0"}}"#,
        )],
    );

    let call = parsed.calls.first().expect("call");
    assert_eq!(call.callee_target_kind.as_str(), "unresolved");
}
