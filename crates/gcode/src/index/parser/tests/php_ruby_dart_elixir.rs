use super::common::{parse_dart, parse_elixir, parse_php, parse_ruby};

/// Asserts a `local_import` call exists for `$callee_name` whose carried
/// candidate files include `$expected_file`. The post-write DB pass narrows
/// these candidates to the real `code_symbols` id.
macro_rules! assert_ruby_local_import {
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
fn leaves_case_insensitive_php_local_symbols_unresolved() {
    let parsed = parse_php(
        r#"
<?php
namespace App\Services;

function run() {
    \APP\SERVICES\MAILER::send();
    \app\services\render();
}
"#,
        &[(
            "src/Services/Mailer.php",
            r#"
<?php
namespace App\Services;

class Mailer {}
function render() {}
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
        .find(|call| {
            call.callee_name == "parse"
                && call.callee_target_kind.as_str() == "external"
                && call.callee_external_module.as_deref() == Some("json")
        })
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
fn resolves_local_ruby_constant_member_and_constructor_calls() {
    let parsed = parse_ruby(
        r#"
require_relative "widget"

def run
  Widget.build("box")
  Widget.new
end
"#,
        &[(
            "lib/widget.rb",
            r#"
class Widget
  def self.build(kind)
  end

  def render
  end
end
"#,
        )],
    );

    // A class-member call resolves against the file declaring the constant; the
    // member name is the resolution target.
    assert_ruby_local_import!(parsed, "build", "lib/widget.rb");
    // `Widget.new` constructs the class, so it resolves to the class symbol
    // (carried under the constant name) rather than a nonexistent `new` method.
    assert_ruby_local_import!(parsed, "Widget", "lib/widget.rb");
}

#[test]
fn classifies_local_ruby_constant_member_and_leaves_receivers_unresolved() {
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

    // A locally-declared constant shadows the bundled gem, so `JSON.parse`
    // becomes a local-import candidate (the post-write pass leaves it unresolved
    // because the local `module JSON` declares no `parse`) rather than external.
    assert_ruby_local_import!(parsed, "parse", "lib/json.rb");
    assert!(
        parsed
            .calls
            .iter()
            .all(|call| call.callee_target_kind.as_str() != "external"),
        "the local JSON constant must suppress the external gem binding"
    );

    // A lowercase receiver is a local variable, and `send` is dynamic dispatch —
    // neither names a constant, so both stay unresolved. Exactly one of the two
    // `parse` calls (the `JSON.parse` one) becomes a local import; the
    // `client.parse` receiver call does not.
    let unresolved_parse = parsed
        .calls
        .iter()
        .filter(|call| call.callee_name == "parse")
        .filter(|call| call.callee_target_kind.as_str() == "unresolved")
        .count();
    assert_eq!(unresolved_parse, 1, "client.parse must stay unresolved");
    let send_call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "send")
        .expect("send call");
    assert_eq!(send_call.callee_target_kind.as_str(), "unresolved");
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
        .find(|call| {
            call.callee_name == "jsonDecode"
                && call.callee_target_kind.as_str() == "external"
                && call.callee_external_module.as_deref() == Some("dart:convert")
        })
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
fn textual_dart_calls_handle_nested_generics() {
    let parsed = parse_dart(
        r#"
void run() {
  builder<Map<String, List<int>>>();
  service.fetch<List<Map<String, int>>>();
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
    assert!(call_names.contains(&"fetch"));
}

#[test]
fn textual_dart_calls_ignore_class_member_declarations() {
    let parsed = parse_dart(
        r#"
abstract class Worker {
  run();
}

void main() {
  run();
}
"#,
        &[],
    );

    let call_names: Vec<_> = parsed
        .calls
        .iter()
        .map(|call| call.callee_name.as_str())
        .collect();
    assert_eq!(call_names, vec!["run"]);
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
fn classifies_unaliased_external_dart_import_bare_calls() {
    let parsed = parse_dart(
        r#"
import 'package:http/http.dart';

void run() {
  Client();
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

    let call = parsed.calls.first().expect("Client call");
    assert_eq!(call.callee_target_kind.as_str(), "external");
    assert_eq!(
        call.callee_external_module.as_deref(),
        Some("package:http/http.dart")
    );
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
fn classifies_external_elixir_imported_bare_calls() {
    let parsed = parse_elixir(
        r#"
defmodule App.Sample do
  import Jason

  def run(body) do
    decode!(body)
  end
end
"#,
        &[("mix.exs", "{:jason, \"~> 1.4\"}\n")],
    );

    let call = parsed.calls.first().expect("decode call");
    assert_eq!(call.callee_target_kind.as_str(), "external");
    assert_eq!(call.callee_external_module.as_deref(), Some("Jason"));
}
