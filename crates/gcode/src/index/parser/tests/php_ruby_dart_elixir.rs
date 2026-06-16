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

/// Asserts a `local_import` call exists for `$callee_name` whose carried
/// candidate files include `$expected_file`. The post-write DB pass narrows
/// these candidates to the real `code_symbols` id.
macro_rules! assert_php_local_import {
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

/// Asserts a `local_import` call exists for `$callee_name` whose carried
/// candidate files include `$expected_file`. Matching on the expected file lets
/// a test distinguish two same-named calls (e.g. an aliased import pair). The
/// post-write DB pass narrows these candidates to the real `code_symbols` id.
macro_rules! assert_dart_local_import {
    ($parsed:expr, $callee_name:expr, $expected_file:expr) => {{
        let callee_name: &str = $callee_name;
        let found = $parsed.calls.iter().any(|call| {
            call.callee_target_kind.as_str() == "local_import"
                && call.callee_name == callee_name
                && call
                    .local_import_candidate_files()
                    .iter()
                    .any(|file| file == $expected_file)
        });
        assert!(
            found,
            "no local_import call for {callee_name} carried candidate file {}; calls: {:?}",
            $expected_file, $parsed.calls
        );
    }};
}

/// Asserts a `local_import` call exists for `$callee_name` whose carried
/// candidate files include `$expected_file`. Matching on the expected file lets
/// a test distinguish calls that share a callee name across modules. The
/// post-write DB pass narrows these candidates to the real `code_symbols` id.
macro_rules! assert_elixir_local_import {
    ($parsed:expr, $callee_name:expr, $expected_file:expr) => {{
        let callee_name: &str = $callee_name;
        let found = $parsed.calls.iter().any(|call| {
            call.callee_target_kind.as_str() == "local_import"
                && call.callee_name == callee_name
                && call
                    .local_import_candidate_files()
                    .iter()
                    .any(|file| file == $expected_file)
        });
        assert!(
            found,
            "no local_import call for {callee_name} carried candidate file {}; calls: {:?}",
            $expected_file, $parsed.calls
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
fn resolves_local_php_use_imported_member_constructor_and_function_calls() {
    let parsed = parse_php(
        r#"
<?php
namespace App;

use App\Widget;
use App\Widget as Gadget;
use function App\helpers\format_label as label;

function run() {
    Widget::build("box");
    new Gadget();
    label("hi");
}
"#,
        &[
            (
                "src/Widget.php",
                r#"
<?php
namespace App;

class Widget {
    public static function build($kind) {}
}
"#,
            ),
            (
                "src/helpers.php",
                r#"
<?php
namespace App\helpers;

function format_label($text) {}
"#,
            ),
        ],
    );

    // `Widget::build()` resolves the static method via the local member channel.
    assert_php_local_import!(&parsed, "build", "src/Widget.php");
    // `new Gadget()` (aliased `use App\Widget as Gadget`) resolves to the class
    // itself via the constructor/bare channel, carrying the real class name.
    assert_php_local_import!(&parsed, "Widget", "src/Widget.php");
    // `label()` (aliased `use function App\helpers\format_label as label`)
    // resolves to the real function name.
    assert_php_local_import!(&parsed, "format_label", "src/helpers.php");
}

#[test]
fn resolves_case_insensitive_php_fully_qualified_static_calls() {
    let parsed = parse_php(
        r#"
<?php
namespace App\Services;

function run() {
    \APP\SERVICES\Mailer::deliver();
    \app\services\render();
}
"#,
        &[(
            "src/Services/Mailer.php",
            r#"
<?php
namespace App\Services;

class Mailer {
    public static function deliver() {}
}
function render() {}
"#,
        )],
    );

    // PHP class names are case-insensitive, so the mis-cased fully-qualified
    // static call still resolves against the declaring file.
    assert_php_local_import!(&parsed, "deliver", "src/Services/Mailer.php");
    // A namespaced free-function call has a namespace-only qualifier (not a
    // declared class), so it is left to same-file/unresolved handling.
    let render = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "render")
        .expect("render call");
    assert_eq!(render.callee_target_kind.as_str(), "unresolved");
}

#[test]
fn leaves_php_dynamic_and_unknown_calls_unresolved() {
    let parsed = parse_php(
        r#"
<?php
namespace App;

use App\Local\Client;

function run($obj) {
    $obj->connect();
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

    // A dynamic instance call (`$obj->`) and calls to undeclared symbols never
    // resolve, even though `Client` is a locally imported class.
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
fn classifies_dart_alias_calls_by_locality() {
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

    // External SDK/package alias calls carry their external module.
    let json_call = parsed
        .calls
        .iter()
        .find(|call| {
            call.callee_name == "jsonDecode"
                && call.callee_target_kind.as_str() == "external"
                && call.callee_external_module.as_deref() == Some("dart:convert")
        })
        .expect("convert.jsonDecode call");
    assert_eq!(json_call.callee_target_kind.as_str(), "external");

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

    // A *local* aliased import (self-package and relative) exposes its file only
    // through the prefix, so `prefix.member()` resolves to a cross-file local
    // import against that file — distinguished here by candidate path.
    assert_dart_local_import!(&parsed, "helper", "lib/local.dart");
    assert_dart_local_import!(&parsed, "helper", "lib/relative.dart");

    // A bare call to a name only reachable through an alias prefix stays
    // unresolved: the unaliased bare scope is empty here.
    let bare_json = parsed
        .calls
        .iter()
        .find(|call| {
            call.callee_name == "jsonDecode" && call.callee_target_kind.as_str() == "unresolved"
        })
        .expect("bare jsonDecode call");
    assert_eq!(bare_json.callee_target_kind.as_str(), "unresolved");
    assert!(parsed.calls.iter().all(|call| call.callee_name != "run"));
}

#[test]
fn resolves_unaliased_local_dart_import_bare_calls() {
    let parsed = parse_dart(
        r#"
import 'package:app/greeter.dart';
import 'widgets/button.dart';

void run() {
  greet();
  Button();
}
"#,
        &[
            ("pubspec.yaml", "name: app\n"),
            ("lib/greeter.dart", "String greet() => 'hi';\n"),
            ("lib/widgets/button.dart", "class Button {}\n"),
        ],
    );

    // Dart unaliased imports expose a file's public top-level symbols to bare
    // calls. A self-package import (`package:app/…` -> `lib/…`) and a relative
    // import each feed the importing file's bare-call candidate set; a free
    // function and a constructor both resolve, pending the post-write DB pass.
    assert_dart_local_import!(&parsed, "greet", "lib/greeter.dart");
    assert_dart_local_import!(&parsed, "Button", "lib/widgets/button.dart");

    // Both are local cross-file calls — neither carries an external module.
    assert!(
        parsed
            .calls
            .iter()
            .filter(|call| matches!(call.callee_name.as_str(), "greet" | "Button"))
            .all(|call| call.callee_target_kind.as_str() == "local_import"),
        "bare calls to imported local symbols must classify as local_import: {:?}",
        parsed.calls
    );

    let run_id = parsed
        .symbols
        .iter()
        .find(|symbol| symbol.file_path == "lib/sample.dart" && symbol.name == "run")
        .map(|symbol| symbol.id.as_str())
        .expect("run function symbol");
    assert!(
        parsed
            .calls
            .iter()
            .filter(|call| matches!(call.callee_name.as_str(), "greet" | "Button"))
            .all(|call| call.caller_symbol_id == run_id),
        "Dart cross-file calls should carry the enclosing caller id: {:?}",
        parsed.calls
    );
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
fn extracts_parameterized_elixir_function_symbols() {
    let parsed = parse_elixir(
        r#"
defmodule App.Sample do
  def greet(name) do
    name
  end

  defp normalize(value) when is_binary(value) do
    String.trim(value)
  end

  defmacro wrap(expr) do
    quote do
      unquote(expr)
    end
  end

  def shout do
    :ok
  end
end
"#,
        &[],
    );

    let symbols: Vec<_> = parsed
        .symbols
        .iter()
        .map(|symbol| (symbol.name.as_str(), symbol.kind.as_str()))
        .collect();

    for expected in [
        ("greet", "function"),
        ("normalize", "function"),
        ("wrap", "function"),
        ("shout", "function"),
    ] {
        assert!(
            symbols.contains(&expected),
            "missing {expected:?}; symbols: {symbols:?}"
        );
    }

    let shout_count = symbols
        .iter()
        .filter(|symbol| symbol.0 == "shout" && symbol.1 == "function")
        .count();
    assert_eq!(
        shout_count, 1,
        "expected exactly one shout function symbol; symbols: {symbols:?}"
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
fn resolves_elixir_local_module_collision_calls_to_declaring_file() {
    // A locally-declared `Jason` module shadows the same-named hex dependency, so
    // both the fully-qualified `Jason.decode!` and the bare `decode!` (imported)
    // bind to the local file rather than the external `Jason` dependency. The
    // post-write DB pass narrows against the file's real symbols.
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

    // Every call binds locally (`local_import`) rather than to the `Jason`
    // dependency — so the local module wins the name collision. The bare and
    // fully-qualified `decode!` calls both carry the local file as their
    // candidate; the post-write DB pass narrows against its real symbols.
    assert!(
        parsed
            .calls
            .iter()
            .all(|call| call.callee_target_kind.as_str() == "local_import"),
        "local collision must bind locally, never to the external Jason dependency; got {:?}",
        parsed.calls
    );
    assert_elixir_local_import!(parsed, "decode!", "lib/jason.ex");
}

#[test]
fn resolves_elixir_fully_qualified_and_aliased_local_calls() {
    let parsed = parse_elixir(
        r#"
defmodule App.Sample do
  alias App.Greeter

  def run(name) do
    App.Helper.format(name)
    Greeter.greet(name)
  end
end
"#,
        &[
            (
                "lib/helper.ex",
                r#"
defmodule App.Helper do
  def format(value) do
    value
  end
end
"#,
            ),
            (
                "lib/greeter.ex",
                r#"
defmodule App.Greeter do
  def greet(name) do
    name
  end
end
"#,
            ),
        ],
    );

    // Fully-qualified `App.Helper.format` resolves the module path directly.
    assert_elixir_local_import!(parsed, "format", "lib/helper.ex");
    // Aliased `Greeter.greet` resolves the local alias through `local_member`.
    assert_elixir_local_import!(parsed, "greet", "lib/greeter.ex");
}

#[test]
fn resolves_elixir_imported_local_bare_calls() {
    let parsed = parse_elixir(
        r#"
defmodule App.Sample do
  import App.Helper

  def run(name) do
    format(name)
  end
end
"#,
        &[(
            "lib/helper.ex",
            r#"
defmodule App.Helper do
  def format(value) do
    value
  end
end
"#,
        )],
    );

    assert_elixir_local_import!(parsed, "format", "lib/helper.ex");
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
