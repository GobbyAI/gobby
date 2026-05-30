use std::fs;
use std::path::{Path, PathBuf};

use tempfile::TempDir;

use super::*;

fn parse_source(file_name: &str, source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
    let tempdir = TempDir::new().expect("create tempdir");
    let root = tempdir.path();
    for (path, contents) in extra_files {
        let file_path = root.join(path);
        if let Some(parent) = file_path.parent() {
            fs::create_dir_all(parent).expect("create parent dirs");
        }
        fs::write(&file_path, contents).expect("write extra source");
    }

    let path = root.join(file_name);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).expect("create parent dirs");
    }
    fs::write(&path, source).expect("write test source");
    let candidates = discover_supported_files(root);
    let context = build_import_resolution_context(root, &candidates);
    parse_file_with_semantic(&path, "proj", root, &[], &context, None)
        .expect("parse result")
        .expect("parse file")
}

fn parse_python(source: &str) -> ParseResult {
    parse_source("sample.py", source, &[])
}

fn parse_javascript(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
    parse_source("src/sample.js", source, extra_files)
}

fn parse_typescript(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
    parse_source("src/sample.ts", source, extra_files)
}

fn parse_go(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
    parse_source("cmd/sample.go", source, extra_files)
}

fn parse_rust(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
    parse_source("src/main.rs", source, extra_files)
}

fn parse_java(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
    parse_source("src/main/java/app/Sample.java", source, extra_files)
}

fn parse_csharp(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
    parse_source("src/Sample.cs", source, extra_files)
}

fn parse_php(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
    parse_source("src/sample.php", source, extra_files)
}

fn parse_ruby(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
    parse_source("lib/sample.rb", source, extra_files)
}

fn parse_dart(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
    parse_source("lib/sample.dart", source, extra_files)
}

fn parse_elixir(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
    parse_source("lib/sample.ex", source, extra_files)
}

fn parse_kotlin(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
    parse_source("src/main/kotlin/Sample.kt", source, extra_files)
}

fn parse_swift(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
    parse_source("Sources/App/main.swift", source, extra_files)
}

fn discover_supported_files(root: &Path) -> Vec<PathBuf> {
    let mut candidates = Vec::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(path) = stack.pop() {
        let entries = fs::read_dir(&path).expect("read dir");
        for entry in entries {
            let entry = entry.expect("dir entry");
            let entry_path = entry.path();
            if entry_path.is_dir() {
                stack.push(entry_path);
            } else if let Some(language) = languages::detect_language(&entry_path.to_string_lossy())
                && !language.is_empty()
            {
                candidates.push(entry_path);
            }
        }
    }
    candidates
}

#[test]
fn line_terminator_len_tracks_lf_crlf_and_eof() {
    let text = "import 'a';\r\nhttp.Client();\nlast()";
    assert_eq!(line_terminator_len(text, 0, "import 'a';".len()), 2);

    let second_start = "import 'a';\r\n".len();
    assert_eq!(
        line_terminator_len(text, second_start, "http.Client();".len()),
        1
    );

    let last_start = "import 'a';\r\nhttp.Client();\n".len();
    assert_eq!(line_terminator_len(text, last_start, "last()".len()), 0);
}

struct FakeSemanticResolver {
    target: Option<crate::index::semantic::SemanticCallTarget>,
    expected_language: &'static str,
    expected_callee: &'static str,
    requests: Vec<CapturedSemanticRequest>,
    error: Option<&'static str>,
}

struct CapturedSemanticRequest {
    language: String,
    file_path: PathBuf,
    root_path: PathBuf,
    callee_name: String,
    line: usize,
    column: usize,
}

impl SemanticCallResolver for FakeSemanticResolver {
    fn resolve(
        &mut self,
        request: &SemanticCallRequest<'_>,
    ) -> anyhow::Result<Option<crate::index::semantic::SemanticCallTarget>> {
        self.requests.push(CapturedSemanticRequest {
            language: request.language.to_string(),
            file_path: request.file_path.to_path_buf(),
            root_path: request.root_path.to_path_buf(),
            callee_name: request.callee_name.to_string(),
            line: request.line,
            column: request.column,
        });
        if let Some(error) = self.error {
            anyhow::bail!("{error}");
        }
        if request.language == self.expected_language && request.callee_name == self.expected_callee
        {
            Ok(self.target.clone())
        } else {
            Ok(None)
        }
    }
}

#[test]
fn explicit_qualified_raw_callee_takes_precedence_over_member_prefix() {
    let mut inferred_called = false;
    let (_, qualifier_from_name) = split_qualified_callee("Vendor\\Pkg\\helper");

    let qualifier_path = call_qualifier_path(qualifier_from_name, || {
        inferred_called = true;
        Some("Vendor".to_string())
    });

    assert_eq!(qualifier_path.as_deref(), Some("Vendor\\Pkg"));
    assert!(!inferred_called);
}

#[test]
fn resolves_unique_same_file_bare_calls() {
    let parsed = parse_python(
        r#"
def foo():
    pass

def bar():
    foo()
"#,
    );

    let foo = parsed
        .symbols
        .iter()
        .find(|symbol| symbol.name == "foo")
        .expect("foo symbol");
    let bar = parsed
        .symbols
        .iter()
        .find(|symbol| symbol.name == "bar")
        .expect("bar symbol");
    let call = parsed.calls.first().expect("call");

    assert_eq!(call.caller_symbol_id, bar.id);
    assert_eq!(call.callee_symbol_id.as_deref(), Some(foo.id.as_str()));
}

#[test]
fn resolves_same_class_member_calls() {
    let parsed = parse_python(
        r#"
class Greeter:
    def greet(self):
        self.render()

    def render(self):
        pass
"#,
    );

    let render = parsed
        .symbols
        .iter()
        .find(|symbol| symbol.qualified_name == "Greeter.render")
        .expect("render method");
    let call = parsed.calls.first().expect("call");

    assert_eq!(call.callee_symbol_id.as_deref(), Some(render.id.as_str()));
}

#[test]
fn leaves_ambiguous_bare_calls_unresolved() {
    let parsed = parse_python(
        r#"
def foo():
    pass

class A:
    def foo(self):
        pass

def bar():
    foo()
"#,
    );

    let call = parsed.calls.first().expect("call");
    assert!(call.callee_symbol_id.is_none());
}

#[test]
fn leaves_non_local_member_calls_unresolved() {
    let parsed = parse_python(
        r#"
class A:
    def bar(self):
        obj.render()

class B:
    def render(self):
        pass
"#,
    );

    let call = parsed.calls.first().expect("call");
    assert!(call.callee_symbol_id.is_none());
}

#[test]
fn classifies_external_python_from_import_calls() {
    let parsed = parse_python(
        r#"
from requests import get as fetch

def run():
    fetch()
"#,
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
fn leaves_self_module_go_imports_unresolved() {
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

    let parse_call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "from_str")
        .expect("from_str call");
    assert_eq!(parse_call.callee_target_kind.as_str(), "external");
    assert_eq!(
        parse_call.callee_external_module.as_deref(),
        Some("serde_json")
    );

    let read_modules: Vec<_> = parsed
        .calls
        .iter()
        .filter(|call| call.callee_name == "read")
        .map(|call| call.callee_external_module.as_deref())
        .collect();
    assert_eq!(read_modules, vec![Some("std::fs"), Some("std::fs")]);
}

#[test]
fn leaves_rust_self_crate_and_glob_imports_unresolved() {
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
    assert!(
        parsed
            .calls
            .iter()
            .all(|call| call.callee_target_kind.as_str() == "unresolved")
    );
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
fn leaves_local_java_imports_unresolved() {
    let parsed = parse_java(
        r#"
package app;

import app.helpers.Helper;

class Sample {
    void run() {
        Helper.render();
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

    let call = parsed.calls.first().expect("call");
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
fn leaves_csharp_instance_and_local_namespace_calls_unresolved() {
    let parsed = parse_csharp(
        r#"
namespace App;

using App.Helpers;

class Sample {
    void Run(Client client) {
        client.Send();
        App.Helpers.Tool.Render();
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
fn classifies_external_php_namespace_and_fully_qualified_calls() {
    let parsed = parse_php(
        r#"
<?php
namespace App;

use Vendor\Pkg\Client as ApiClient;
use function Vendor\Pkg\do_work as work;

function run() {
    ApiClient::connect();
    work();
    \Vendor\Pkg\helper();
    \Vendor\Pkg\Service::build();
}
"#,
        &[],
    );

    let static_call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "connect")
        .expect("static call");
    assert_eq!(static_call.callee_target_kind.as_str(), "external");
    assert_eq!(
        static_call.callee_external_module.as_deref(),
        Some("Vendor\\Pkg\\Client")
    );

    let function_import_call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "do_work")
        .expect("function import call");
    assert_eq!(function_import_call.callee_target_kind.as_str(), "external");
    assert_eq!(
        function_import_call.callee_external_module.as_deref(),
        Some("Vendor\\Pkg")
    );

    let qualified_function_call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "helper")
        .expect("qualified function call");
    assert_eq!(
        qualified_function_call.callee_target_kind.as_str(),
        "external"
    );
    assert_eq!(
        qualified_function_call.callee_external_module.as_deref(),
        Some("Vendor\\Pkg")
    );

    let qualified_static_call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "build")
        .expect("qualified static call");
    assert_eq!(
        qualified_static_call.callee_target_kind.as_str(),
        "external"
    );
    assert_eq!(
        qualified_static_call.callee_external_module.as_deref(),
        Some("Vendor\\Pkg\\Service")
    );
}

#[test]
fn leaves_php_dynamic_member_and_local_import_calls_unresolved() {
    let parsed = parse_php(
        r#"
<?php
namespace App;

use App\Local\Client;

function run($obj) {
    $obj->connect();
    Client::connect();
    \missing();
    missing();
}
"#,
        &[(
            "src/Local/Client.php",
            r#"
<?php
namespace App\Local;

class Client {}
"#,
        )],
    );

    assert!(
        parsed
            .calls
            .iter()
            .all(|call| call.callee_target_kind.as_str() == "unresolved")
    );
}

#[test]
fn classifies_external_ruby_constant_qualified_require_calls() {
    let parsed = parse_ruby(
        r#"
require "json"
require "fileutils"

def run
  JSON.parse("{}")
  FileUtils.mkdir_p("tmp")
  parse("{}")
end
"#,
        &[],
    );

    let json_call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "parse")
        .expect("json call");
    assert_eq!(json_call.callee_target_kind.as_str(), "external");
    assert_eq!(json_call.callee_external_module.as_deref(), Some("json"));

    let mkdir_call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "mkdir_p")
        .expect("fileutils call");
    assert_eq!(mkdir_call.callee_target_kind.as_str(), "external");
    assert_eq!(
        mkdir_call.callee_external_module.as_deref(),
        Some("fileutils")
    );
}

#[test]
fn leaves_ruby_local_constant_collision_and_receivers_unresolved() {
    let parsed = parse_ruby(
        r#"
require "json"

def run(client)
  JSON.parse("{}")
  client.parse("{}")
  send(:parse, "{}")
end
"#,
        &[(
            "lib/json.rb",
            r#"
module JSON
end
"#,
        )],
    );

    assert!(
        parsed
            .calls
            .iter()
            .all(|call| call.callee_target_kind.as_str() == "unresolved")
    );
}

#[test]
fn classifies_external_dart_alias_calls_only() {
    let parsed = parse_dart(
        r#"
import 'dart:convert' as convert;
import 'package:http/http.dart' as http show Client;
import 'package:app/local.dart' as local;
import './relative.dart' as relative;

void run() {
  convert.jsonDecode("{}");
  http.Client();
  local.helper();
  relative.helper();
  jsonDecode("{}");
}
"#,
        &[(
            "pubspec.yaml",
            r#"
name: app
dependencies:
  http: ^1.0.0
"#,
        )],
    );

    let json_call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "jsonDecode")
        .expect("jsonDecode call");
    assert_eq!(json_call.callee_target_kind.as_str(), "external");
    assert_eq!(
        json_call.callee_external_module.as_deref(),
        Some("dart:convert")
    );

    let client_call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "Client")
        .expect("Client call");
    assert_eq!(client_call.callee_target_kind.as_str(), "external");
    assert_eq!(
        client_call.callee_external_module.as_deref(),
        Some("package:http/http.dart")
    );

    let unresolved: Vec<_> = parsed
        .calls
        .iter()
        .filter(|call| matches!(call.callee_name.as_str(), "helper" | "jsonDecode"))
        .filter(|call| call.callee_target_kind.as_str() == "unresolved")
        .collect();
    assert_eq!(unresolved.len(), 3);
    assert!(parsed.calls.iter().all(|call| call.callee_name != "run"));
}

#[test]
fn textual_dart_calls_handle_generics_and_ignore_comments_and_strings() {
    let parsed = parse_dart(
        r#"
void run() {
  builder<T>();
  final text = "fakeCall()";
  final other = 'otherCall()';
  // commentedCall();
  /* blockCall();
     stillBlockCall();
  */
  afterBlock(); // trailingCommentCall();
}
"#,
        &[],
    );

    let call_names: Vec<_> = parsed
        .calls
        .iter()
        .map(|call| call.callee_name.as_str())
        .collect();
    assert!(call_names.contains(&"builder"));
    assert!(call_names.contains(&"afterBlock"));
    for skipped in [
        "fakeCall",
        "otherCall",
        "commentedCall",
        "blockCall",
        "stillBlockCall",
        "trailingCommentCall",
    ] {
        assert!(!call_names.contains(&skipped), "unexpected call {skipped}");
    }
}

#[test]
fn textual_dart_calls_ignore_raw_and_triple_quoted_multiline_strings() {
    let parsed = parse_dart(
        r#"
void run() {
  final raw = r"rawCall()";
  final triple = '''
    tripleCall();
  ''';
  final rawTriple = r"""
    rawTripleCall();
  """;
  afterStrings();
}
"#,
        &[],
    );

    let call_names: Vec<_> = parsed
        .calls
        .iter()
        .map(|call| call.callee_name.as_str())
        .collect();
    assert_eq!(call_names, vec!["afterStrings"]);
}

#[test]
fn classifies_external_elixir_remote_alias_and_required_calls() {
    let parsed = parse_elixir(
        r#"
defmodule App.Sample do
  alias HTTPoison, as: HTTP
  require Jason

  def run(body) do
    Jason.decode!(body)
    HTTP.get("https://example.com")
  end
end
"#,
        &[
            (
                "mix.exs",
                r#"
defmodule App.MixProject do
  defp deps do
    [
      {:jason, "~> 1.4"},
      {:httpoison, "~> 2.0"}
    ]
  end
end
"#,
            ),
            (
                "mix.lock",
                r#"{"jason": {:hex, :jason}, "httpoison": {:hex, :httpoison}}"#,
            ),
        ],
    );

    let decode_call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "decode!")
        .expect("decode call");
    assert_eq!(decode_call.callee_target_kind.as_str(), "external");
    assert_eq!(decode_call.callee_external_module.as_deref(), Some("Jason"));

    let get_call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "get")
        .expect("get call");
    assert_eq!(get_call.callee_target_kind.as_str(), "external");
    assert_eq!(
        get_call.callee_external_module.as_deref(),
        Some("HTTPoison")
    );
}

#[test]
fn leaves_elixir_local_module_collision_and_imported_calls_unresolved() {
    let parsed = parse_elixir(
        r#"
defmodule App.Sample do
  import Jason

  def run(body) do
    Jason.decode!(body)
    decode!(body)
  end
end
"#,
        &[
            ("mix.exs", "{:jason, \"~> 1.4\"}\n"),
            (
                "lib/jason.ex",
                r#"
defmodule Jason do
end
"#,
            ),
        ],
    );

    assert!(
        parsed
            .calls
            .iter()
            .all(|call| call.callee_target_kind.as_str() == "unresolved")
    );
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

#[test]
fn semantic_resolver_can_classify_cpp_calls_as_external() {
    let tempdir = TempDir::new().expect("create tempdir");
    let root = tempdir.path();
    let path = root.join("src/main.cpp");
    fs::create_dir_all(path.parent().expect("parent")).expect("create parent dirs");
    fs::write(
        &path,
        r#"
void run() {
    printf("x");
}
"#,
    )
    .expect("write source");
    let candidates = discover_supported_files(root);
    let context = build_import_resolution_context(root, &candidates);
    let mut resolver = FakeSemanticResolver {
        target: Some(crate::index::semantic::SemanticCallTarget {
            callee_name: "printf".to_string(),
            external_module: "/usr/include/stdio.h".to_string(),
        }),
        expected_language: "cpp",
        expected_callee: "printf",
        requests: Vec::new(),
        error: None,
    };
    let parsed = parse_file_with_semantic(&path, "proj", root, &[], &context, Some(&mut resolver))
        .expect("parse result")
        .expect("parse file");

    let call = parsed.calls.first().expect("printf call");
    assert_eq!(call.callee_target_kind.as_str(), "external");
    assert_eq!(
        call.callee_external_module.as_deref(),
        Some("/usr/include/stdio.h")
    );
}

#[test]
fn semantic_resolver_can_classify_textual_dart_calls_as_external() {
    let tempdir = TempDir::new().expect("create tempdir");
    let root = tempdir.path();
    let path = root.join("lib/sample.dart");
    fs::create_dir_all(path.parent().expect("parent")).expect("create parent dirs");
    fs::write(
        &path,
        r#"
void run() {
  Tooltip(message: 'x');
}
"#,
    )
    .expect("write source");
    let candidates = discover_supported_files(root);
    let context = build_import_resolution_context(root, &candidates);
    let mut resolver = FakeSemanticResolver {
        target: Some(crate::index::semantic::SemanticCallTarget {
            callee_name: "Tooltip".to_string(),
            external_module: "package:flutter/material.dart".to_string(),
        }),
        expected_language: "dart",
        expected_callee: "Tooltip",
        requests: Vec::new(),
        error: None,
    };
    let parsed = parse_file_with_semantic(&path, "proj", root, &[], &context, Some(&mut resolver))
        .expect("parse result")
        .expect("parse file");

    let call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "Tooltip")
        .expect("Tooltip call");
    assert_eq!(call.callee_target_kind.as_str(), "external");
    assert_eq!(
        call.callee_external_module.as_deref(),
        Some("package:flutter/material.dart")
    );
    assert!(resolver.requests.iter().any(|request| {
        request.language == "dart"
            && request.file_path == path
            && request.root_path == root
            && request.callee_name == "Tooltip"
    }));
}

#[test]
fn semantic_resolver_receives_utf16_columns_for_ast_calls() {
    let tempdir = TempDir::new().expect("create tempdir");
    let root = tempdir.path();
    let path = root.join("src/main.cpp");
    fs::create_dir_all(path.parent().expect("parent")).expect("create parent dirs");
    let source = format!(
        "void run() {{\n    auto s = \"{}\"; printf(\"x\");\n}}\n",
        '\u{1F600}'
    );
    fs::write(&path, source.as_bytes()).expect("write source");
    let candidates = discover_supported_files(root);
    let context = build_import_resolution_context(root, &candidates);
    let mut resolver = FakeSemanticResolver {
        target: None,
        expected_language: "cpp",
        expected_callee: "printf",
        requests: Vec::new(),
        error: None,
    };

    parse_file_with_semantic(&path, "proj", root, &[], &context, Some(&mut resolver))
        .expect("parse result")
        .expect("parse file");

    let request = resolver
        .requests
        .iter()
        .find(|request| request.callee_name == "printf")
        .expect("printf semantic request");
    let prefix = format!("    auto s = \"{}\"; ", '\u{1F600}');
    assert_eq!(request.line, 2);
    assert_eq!(request.column, prefix.encode_utf16().count());
}

#[test]
fn semantic_resolver_receives_utf16_columns_for_textual_dart_calls() {
    let tempdir = TempDir::new().expect("create tempdir");
    let root = tempdir.path();
    let path = root.join("lib/sample.dart");
    fs::create_dir_all(path.parent().expect("parent")).expect("create parent dirs");
    let source = format!(
        "void run() {{\n  final s = '{}'; Tooltip(message: 'x');\n}}\n",
        '\u{1F600}'
    );
    fs::write(&path, source.as_bytes()).expect("write source");
    let candidates = discover_supported_files(root);
    let context = build_import_resolution_context(root, &candidates);
    let mut resolver = FakeSemanticResolver {
        target: None,
        expected_language: "dart",
        expected_callee: "Tooltip",
        requests: Vec::new(),
        error: None,
    };

    parse_file_with_semantic(&path, "proj", root, &[], &context, Some(&mut resolver))
        .expect("parse result")
        .expect("parse file");

    let request = resolver
        .requests
        .iter()
        .find(|request| request.callee_name == "Tooltip")
        .expect("Tooltip semantic request");
    let prefix = format!("  final s = '{}'; ", '\u{1F600}');
    assert_eq!(request.line, 2);
    assert_eq!(request.column, prefix.encode_utf16().count());
}

#[test]
fn semantic_resolver_errors_are_propagated() {
    let tempdir = TempDir::new().expect("create tempdir");
    let root = tempdir.path();
    let path = root.join("src/main.cpp");
    fs::create_dir_all(path.parent().expect("parent")).expect("create parent dirs");
    fs::write(
        &path,
        r#"
void run() {
    printf("x");
}
"#,
    )
    .expect("write source");
    let candidates = discover_supported_files(root);
    let context = build_import_resolution_context(root, &candidates);
    let mut resolver = FakeSemanticResolver {
        target: None,
        expected_language: "cpp",
        expected_callee: "printf",
        requests: Vec::new(),
        error: Some("semantic resolver failed"),
    };

    let err =
        match parse_file_with_semantic(&path, "proj", root, &[], &context, Some(&mut resolver)) {
            Err(err) => err,
            Ok(_) => panic!("expected semantic resolver error"),
        };

    assert_eq!(err.to_string(), "semantic resolver failed");
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
