use super::common::{parse_javascript, parse_python, parse_source, parse_tsx, parse_typescript};
use crate::models::CallRelation;

/// A cross-file local import is now recorded at parse time as a pending
/// `local_import` call: the original name plus the candidate target files. The
/// canonical symbol id is resolved later against `code_symbols` (covered by the
/// live `graph_standalone` integration test), so the parser-level contract is
/// the `local_import` shape and that the real target file is among the
/// candidates the post-write resolver will search.
///
/// A macro (rather than a function) so the assertions expand directly into each
/// test body.
macro_rules! assert_local_import_call {
    ($call:expr, $callee_name:expr, $expected_file:expr) => {{
        let call: &CallRelation = $call;
        assert_eq!(
            call.callee_target_kind.as_str(),
            "local_import",
            "expected a local_import call, got {} (module {:?})",
            call.callee_target_kind.as_str(),
            call.callee_external_module
        );
        assert_eq!(call.callee_name, $callee_name);
        let candidates = call.local_import_candidate_files();
        assert!(
            candidates.iter().any(|file| file == $expected_file),
            "candidate files {candidates:?} did not contain {}",
            $expected_file
        );
    }};
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
fn records_local_python_imports_as_local_import_calls() {
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
    assert_local_import_call!(call, "helper", "pkg/utils.py");
}

#[test]
fn parse_records_local_import_without_reading_target_file_contents() {
    // The retired file-scan approach only created a local binding when the
    // imported name was found by reading and line-scanning the target file. The
    // post-write design derives candidate files from the import path alone, so a
    // local import is recorded even when the target file does NOT define the
    // name (it degrades to unresolved during the DB pass). This is the
    // parse-time guarantee that indexing performs no per-source-file scan (#790).
    let parsed = parse_source(
        "pkg/main.py",
        r#"
from pkg.utils import not_defined_here

def run():
    not_defined_here()
"#,
        // The target exists (so `pkg.utils` classifies as local) but does not
        // define `not_defined_here`; the old scanner would have left this
        // unresolved.
        &[("pkg/utils.py", "def something_else():\n    pass\n")],
    );

    let call = parsed.calls.first().expect("call");
    assert_local_import_call!(call, "not_defined_here", "pkg/utils.py");
}

#[test]
fn records_aliased_relative_python_imports_as_local_import_calls() {
    let parsed = parse_source(
        "pkg/main.py",
        r#"
from .service import Service as ApiService

def run():
    ApiService()
"#,
        &[("pkg/service.py", "class Service:\n    pass\n")],
    );

    // `.service` normalizes against the caller's package, and the alias resolves
    // back to the original `Service` name for the post-write lookup.
    let call = parsed.calls.first().expect("call");
    assert_local_import_call!(call, "Service", "pkg/service.py");
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
fn records_local_javascript_named_import_aliases_as_local_import_calls() {
    let parsed = parse_javascript(
        r#"
import { helper as runHelper } from "./utils";

function run() {
runHelper();
}
"#,
        &[("src/utils.js", "export function helper() {}\n")],
    );

    // The alias `runHelper` resolves back to the original export name `helper`.
    let call = parsed.calls.first().expect("call");
    assert_local_import_call!(call, "helper", "src/utils.js");
}

#[test]
fn leaves_local_javascript_default_imports_unresolved() {
    // A default export carries its own declared name, which the parse-time
    // binding cannot know, so default imports degrade to unresolved rather than
    // record a guaranteed-miss local_import. (A named export of the same symbol
    // resolves fine — see the named-import test.)
    let parsed = parse_javascript(
        r#"
import helper from "./utils";

function run() {
helper();
}
"#,
        &[("src/utils.js", "export default function helper() {}\n")],
    );

    let call = parsed.calls.first().expect("call");
    assert_eq!(call.callee_target_kind.as_str(), "unresolved");
    assert!(call.callee_external_module.is_none());
}

#[test]
fn records_local_javascript_namespace_member_calls_as_local_import_calls() {
    let parsed = parse_javascript(
        r#"
import * as Utils from "./utils";

function run() {
Utils.helper();
}
"#,
        &[(
            "src/utils.js",
            "export function helper() {}\nexport function other() {}\n",
        )],
    );

    let call = parsed.calls.first().expect("call");
    assert_local_import_call!(call, "helper", "src/utils.js");
}

#[test]
fn records_self_package_javascript_imports_as_local_import_calls() {
    let parsed = parse_javascript(
        r#"
import { helper } from "app/utils";

function run() {
helper();
}
"#,
        &[
            ("package.json", r#"{"name":"app","dependencies":{}}"#),
            ("src/utils.js", "export function helper() {}\n"),
        ],
    );

    let call = parsed.calls.first().expect("call");
    assert_local_import_call!(call, "helper", "src/utils.js");
}

#[test]
fn records_local_typescript_named_import_aliases_as_local_import_calls() {
    let parsed = parse_typescript(
        r#"
import { loadUser as load } from "./api";

export function run(): void {
load();
}
"#,
        &[("src/api.ts", "export function loadUser(): void {}\n")],
    );

    let call = parsed.calls.first().expect("call");
    assert_local_import_call!(call, "loadUser", "src/api.ts");
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
fn records_javascript_at_path_alias_imports_as_local_import_calls() {
    // `@/` (and `~/`) are the conventional `src/` path aliases, so `@/utils`
    // resolves to `src/utils.*` as a local import rather than an external
    // package; the post-write pass narrows it against the indexed symbols.
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
    assert_local_import_call!(call, "helper", "src/utils.js");
}

/// #791 no-phantom regression (services-free half): a `@decorator`-wrapped
/// function and a doc-commented class are extracted as top-level symbols with
/// their original names — the keys the post-write resolver matches on — and a
/// sibling module's import of them (one aliased) is recorded as a resolvable
/// `local_import`. Post-#790 the resolver matches on the imported name, not a
/// recomputed byte offset, so the decorator/docstring offset shift cannot desync
/// the target id (the failure mode the retired file-scan pre-scan risked).
#[test]
fn decorated_python_definitions_extract_and_resolve_as_local_imports() {
    let defs = r#"
def _mark(fn):
    return fn


@_mark
def decorated_target():
    """Doc-commented, decorated target."""
    return 1


class DecoratedService:
    """Doc-commented service."""

    def __init__(self):
        pass
"#;

    let target = parse_python(defs, &[]);
    for (name, kind) in [
        ("decorated_target", "function"),
        ("DecoratedService", "class"),
    ] {
        let symbol = target
            .symbols
            .iter()
            .find(|symbol| symbol.name == name)
            .unwrap_or_else(|| panic!("missing top-level {name}"));
        assert_eq!(symbol.kind.as_str(), kind, "{name} kind");
        assert!(
            symbol.parent_symbol_id.is_none(),
            "{name} should be a top-level symbol"
        );
    }

    let caller = parse_source(
        "caller.py",
        r#"
from defs import decorated_target
from defs import DecoratedService as Svc

def run():
    decorated_target()
    Svc()
"#,
        &[("defs.py", defs)],
    );
    let decorated = caller
        .calls
        .iter()
        .find(|call| call.callee_name == "decorated_target")
        .expect("decorated_target call");
    assert_local_import_call!(decorated, "decorated_target", "defs.py");
    let service = caller
        .calls
        .iter()
        .find(|call| call.callee_name == "DecoratedService")
        .expect("aliased Svc call");
    assert_local_import_call!(service, "DecoratedService", "defs.py");
}

/// #791 no-phantom regression (services-free half), TS arm: a JSDoc-commented
/// export is extracted as a top-level symbol, and a sibling module importing it
/// is recorded as a resolvable `local_import` pointing at the defining file.
#[test]
fn documented_typescript_definitions_extract_and_resolve_as_local_imports() {
    let defs = "/** Doc-commented target. */\nexport function tsTarget(): void {}\n";

    let target = parse_source("src/api.ts", defs, &[]);
    let symbols = target
        .symbols
        .iter()
        .filter(|symbol| symbol.name == "tsTarget")
        .collect::<Vec<_>>();
    assert_eq!(
        symbols.len(),
        1,
        "expected one tsTarget symbol: {symbols:?}"
    );
    let symbol = symbols[0];
    assert_eq!(symbol.kind.as_str(), "function");
    assert!(
        symbol.parent_symbol_id.is_none(),
        "tsTarget should be a top-level symbol"
    );

    let caller = parse_source(
        "src/caller.ts",
        "import { tsTarget } from \"./api\";\n\nexport function run(): void {\n  tsTarget();\n}\n",
        &[("src/api.ts", defs)],
    );
    let call = caller.calls.first().expect("call");
    assert_local_import_call!(call, "tsTarget", "src/api.ts");
}

/// #791 no-phantom regression (services-free half), JS arm: the JavaScript
/// grammar uses a `declaration` supertype under exported declarations, so a
/// JSDoc-commented export must still be extracted as the real target symbol
/// that the post-write resolver matches.
#[test]
fn documented_javascript_definitions_extract_and_resolve_as_local_imports() {
    let defs = "/** Doc-commented target. */\nexport function jsTarget() {}\n";

    let target = parse_source("src/api.js", defs, &[]);
    let symbols = target
        .symbols
        .iter()
        .filter(|symbol| symbol.name == "jsTarget")
        .collect::<Vec<_>>();
    assert_eq!(
        symbols.len(),
        1,
        "expected one jsTarget symbol: {symbols:?}"
    );
    let symbol = symbols[0];
    assert_eq!(symbol.kind.as_str(), "function");
    assert!(
        symbol.parent_symbol_id.is_none(),
        "jsTarget should be a top-level symbol"
    );

    let caller = parse_source(
        "src/caller.js",
        "import { jsTarget as runJsTarget } from \"./api\";\n\nexport function run() {\n  runJsTarget();\n}\n",
        &[("src/api.js", defs)],
    );
    let call = caller.calls.first().expect("call");
    assert_local_import_call!(call, "jsTarget", "src/api.js");
}
