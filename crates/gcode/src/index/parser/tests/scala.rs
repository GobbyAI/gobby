use super::common::{parse_scala, parse_source};

macro_rules! assert_scala_local_import {
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
fn extracts_scala_symbols_imports_and_calls() {
    let parsed = parse_scala(
        r#"
package app

import scala.concurrent.Future

trait Service {
  def run(): Unit
}

class Runner extends Service {
  def run(): Unit = {
    Future.successful(())
  }
}

object Main {
  def main(): Unit = {
    Runner()
  }
}
"#,
        &[],
    );

    assert!(
        parsed
            .symbols
            .iter()
            .any(|symbol| symbol.name == "Service" && symbol.kind == "type")
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
            .any(|symbol| symbol.name == "Main" && symbol.kind == "class")
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
            .any(|import| import.module_name == "scala.concurrent.Future")
    );
    let future_call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "successful")
        .expect("Future.successful call");
    assert_eq!(future_call.callee_target_kind.as_str(), "external");
    assert_eq!(
        future_call.callee_external_module.as_deref(),
        Some("scala.concurrent.Future")
    );
}

#[test]
fn resolves_scala_single_and_grouped_local_import_calls() {
    let parsed = parse_scala(
        r#"
package app

import app.helpers.render
import app.widgets.{Widget, buildWidget}

object Main {
  def run(): Unit = {
    render()
    Widget.build()
    new Widget()
    buildWidget()
  }
}
"#,
        &[
            (
                "src/main/scala/helpers/Helpers.scala",
                "package app.helpers\n\ndef render(): Unit = {}\n",
            ),
            (
                "src/main/scala/widgets/Widget.scala",
                "package app.widgets\n\nclass Widget {\n  def build(): Unit = {}\n}\ndef buildWidget(): Widget = new Widget()\n",
            ),
        ],
    );

    assert!(
        parsed
            .imports
            .iter()
            .any(|import| import.module_name == "app.helpers.render")
    );
    assert!(
        parsed
            .imports
            .iter()
            .any(|import| import.module_name == "app.widgets.Widget")
    );
    assert!(
        parsed
            .imports
            .iter()
            .any(|import| import.module_name == "app.widgets.buildWidget")
    );
    assert_scala_local_import!(&parsed, "render", "src/main/scala/helpers/Helpers.scala");
    assert_scala_local_import!(&parsed, "build", "src/main/scala/widgets/Widget.scala");
    assert_scala_local_import!(&parsed, "Widget", "src/main/scala/widgets/Widget.scala");
    assert_scala_local_import!(
        &parsed,
        "buildWidget",
        "src/main/scala/widgets/Widget.scala"
    );
}

#[test]
fn resolves_aliased_scala_imports_without_phantom_wildcard_edges() {
    let parsed = parse_scala(
        r#"
package app

import app.widgets.{Widget => W, buildWidget as makeWidget, _}

object Main {
  def run(): Unit = {
    W.build()
    new W()
    makeWidget()
    wildcardOnly()
  }
}
"#,
        &[(
            "src/main/scala/widgets/Widget.scala",
            "package app.widgets\n\nclass Widget {\n  def build(): Unit = {}\n}\ndef buildWidget(): Widget = new Widget()\ndef wildcardOnly(): Unit = {}\n",
        )],
    );

    assert_scala_local_import!(&parsed, "build", "src/main/scala/widgets/Widget.scala");
    assert_scala_local_import!(&parsed, "Widget", "src/main/scala/widgets/Widget.scala");
    assert_scala_local_import!(
        &parsed,
        "buildWidget",
        "src/main/scala/widgets/Widget.scala"
    );
    assert!(
        parsed.calls.iter().any(|call| {
            call.callee_name == "wildcardOnly" && call.callee_target_kind.as_str() == "unresolved"
        }),
        "wildcard-only import must not create a local_import edge: {:?}",
        parsed.calls
    );
}

#[test]
fn detects_scala_script_extension() {
    let parsed = parse_source(
        "scripts/build.sc",
        r#"
def compile(): Unit = println("compile")
compile()
"#,
        &[],
    );

    assert!(
        parsed
            .symbols
            .iter()
            .any(|symbol| symbol.name == "compile" && symbol.kind == "function")
    );
    assert!(
        parsed
            .calls
            .iter()
            .any(|call| call.callee_name == "compile")
    );
}
