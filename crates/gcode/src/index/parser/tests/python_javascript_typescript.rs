use super::common::{parse_javascript, parse_python, parse_source, parse_tsx, parse_typescript};
use crate::models::{CallRelation, ParseResult, Symbol};

const TEST_PROJECT_ID: &str = "proj";

fn canonical_symbol_id(file_path: &str, name: &str, kind: &str, byte_start: usize) -> String {
    Symbol::make_id(TEST_PROJECT_ID, file_path, name, kind, byte_start)
}

fn assert_call_targets_parsed_symbol(
    call: &CallRelation,
    target: &ParseResult,
    name: &str,
    kind: &str,
) {
    let matching_ids = target
        .symbols
        .iter()
        .filter(|symbol| symbol.name == name && symbol.kind == kind)
        .map(|symbol| symbol.id.as_str())
        .collect::<Vec<_>>();
    assert!(
        !matching_ids.is_empty(),
        "missing target symbol {name}/{kind}; symbols: {:?}",
        target
            .symbols
            .iter()
            .map(|symbol| (&symbol.name, &symbol.kind, &symbol.id))
            .collect::<Vec<_>>()
    );
    assert_eq!(call.callee_target_kind.as_str(), "symbol");
    assert!(
        matching_ids
            .iter()
            .any(|id| Some(*id) == call.callee_symbol_id.as_deref()),
        "call target {:?} was not one of {matching_ids:?}",
        call.callee_symbol_id
    );
    assert!(call.callee_external_module.is_none());
}

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
fn resolves_direct_python_calls_to_canonical_symbol() {
    let parsed = parse_python(
        r#"
def helper():
    pass

def run():
    helper()
"#,
        &[],
    );

    let helper = parsed
        .symbols
        .iter()
        .find(|symbol| symbol.name == "helper")
        .expect("helper symbol");
    let call = parsed.calls.first().expect("call");

    assert_eq!(call.callee_target_kind.as_str(), "symbol");
    assert_eq!(call.callee_symbol_id.as_deref(), Some(helper.id.as_str()));
}

#[test]
fn resolves_local_python_imports_to_canonical_symbol() {
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
    let helper_id = canonical_symbol_id("pkg/utils.py", "helper", "function", 0);

    assert_eq!(call.callee_target_kind.as_str(), "symbol");
    assert_eq!(call.callee_symbol_id.as_deref(), Some(helper_id.as_str()));
    assert!(call.callee_external_module.is_none());
}

#[test]
fn resolves_aliased_local_python_imports_to_canonical_symbol() {
    let parsed = parse_source(
        "pkg/main.py",
        r#"
from .service import Service as ApiService

def run():
    ApiService()
"#,
        &[("pkg/service.py", "class Service:\n    pass\n")],
    );

    let call = parsed.calls.first().expect("call");
    let service_id = canonical_symbol_id("pkg/service.py", "Service", "class", 0);

    assert_eq!(call.callee_target_kind.as_str(), "symbol");
    assert_eq!(call.callee_symbol_id.as_deref(), Some(service_id.as_str()));
    assert!(call.callee_external_module.is_none());
}

#[test]
fn resolves_python_class_constructor_calls_to_canonical_symbol() {
    let parsed = parse_python(
        r#"
class Service:
    pass

def run():
    Service()
"#,
        &[],
    );

    let service = parsed
        .symbols
        .iter()
        .find(|symbol| symbol.name == "Service")
        .expect("Service symbol");
    let call = parsed.calls.first().expect("call");

    assert_eq!(call.callee_target_kind.as_str(), "symbol");
    assert_eq!(call.callee_symbol_id.as_deref(), Some(service.id.as_str()));
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
fn resolves_local_javascript_named_import_aliases_to_canonical_symbol() {
    let target_source = "export function helper() {}\n";
    let parsed = parse_javascript(
        r#"
import { helper as runHelper } from "./utils";

function run() {
runHelper();
}
"#,
        &[("src/utils.js", target_source)],
    );
    let target = parse_source("src/utils.js", target_source, &[]);

    let call = parsed.calls.first().expect("call");
    assert_eq!(call.callee_target_kind.as_str(), "symbol");
    assert_call_targets_parsed_symbol(call, &target, "helper", "function");
}

#[test]
fn resolves_local_javascript_default_imports_to_canonical_symbol() {
    let target_source = "export default function helper() {}\n";
    let parsed = parse_javascript(
        r#"
import helper from "./utils";

function run() {
helper();
}
"#,
        &[("src/utils.js", target_source)],
    );
    let target = parse_source("src/utils.js", target_source, &[]);

    let call = parsed.calls.first().expect("call");
    assert_eq!(call.callee_target_kind.as_str(), "symbol");
    assert_call_targets_parsed_symbol(call, &target, "helper", "function");
}

#[test]
fn resolves_local_javascript_namespace_member_calls_to_canonical_symbol() {
    let target_source = "export function helper() {}\nexport function other() {}\n";
    let parsed = parse_javascript(
        r#"
import * as Utils from "./utils";

function run() {
Utils.helper();
}
"#,
        &[("src/utils.js", target_source)],
    );
    let target = parse_source("src/utils.js", target_source, &[]);

    let call = parsed.calls.first().expect("call");
    assert_eq!(call.callee_target_kind.as_str(), "symbol");
    assert_call_targets_parsed_symbol(call, &target, "helper", "function");
}

#[test]
fn resolves_self_package_javascript_imports_to_canonical_symbol() {
    let target_source = "export function helper() {}\n";
    let parsed = parse_javascript(
        r#"
import { helper } from "app/utils";

function run() {
helper();
}
"#,
        &[
            ("package.json", r#"{"name":"app","dependencies":{}}"#),
            ("src/utils.js", target_source),
        ],
    );
    let target = parse_source("src/utils.js", target_source, &[]);

    let call = parsed.calls.first().expect("call");
    assert_eq!(call.callee_target_kind.as_str(), "symbol");
    assert_call_targets_parsed_symbol(call, &target, "helper", "function");
}

#[test]
fn resolves_local_typescript_named_import_aliases_to_canonical_symbol() {
    let target_source = "export function loadUser(): void {}\n";
    let parsed = parse_typescript(
        r#"
import { loadUser as load } from "./api";

export function run(): void {
load();
}
"#,
        &[("src/api.ts", target_source)],
    );
    let target = parse_source("src/api.ts", target_source, &[]);

    let call = parsed.calls.first().expect("call");
    assert_eq!(call.callee_target_kind.as_str(), "symbol");
    assert_call_targets_parsed_symbol(call, &target, "loadUser", "function");
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
