use super::common::{parse_csharp, parse_go, parse_java, parse_rust};

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
