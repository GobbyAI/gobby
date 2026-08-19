use super::common::{
    parse_csharp, parse_dart, parse_elixir, parse_go, parse_java, parse_javascript, parse_kotlin,
    parse_lua, parse_objc, parse_php, parse_python, parse_ruby, parse_rust, parse_scala,
    parse_source, parse_swift, parse_typescript,
};
use crate::models::{CallTargetKind, HeritageKind, InheritanceRelation, ParseResult};

fn type_symbol_id(parsed: &ParseResult, name: &str) -> String {
    parsed
        .symbols
        .iter()
        .find(|symbol| symbol.name == name && matches!(symbol.kind.as_str(), "class" | "type"))
        .unwrap_or_else(|| panic!("missing type symbol {name}: {:?}", parsed.symbols))
        .id
        .clone()
}

fn heritage_row<'a>(
    parsed: &'a ParseResult,
    source: &str,
    target: &str,
) -> &'a InheritanceRelation {
    parsed
        .inheritance
        .iter()
        .find(|row| row.source_name == source && row.target_name == target)
        .unwrap_or_else(|| {
            let file = parsed
                .symbols
                .first()
                .map(|symbol| symbol.file_path.as_str())
                .unwrap_or("<unknown>");
            panic!(
                "missing {source} -> {target} heritage row in {file} (symbols {:?}): {:?}",
                parsed
                    .symbols
                    .iter()
                    .map(|symbol| (symbol.name.as_str(), symbol.kind.as_str()))
                    .collect::<Vec<_>>(),
                parsed
                    .inheritance
                    .iter()
                    .map(|row| (
                        row.source_name.as_str(),
                        row.target_name.as_str(),
                        row.heritage_kind
                    ))
                    .collect::<Vec<_>>()
            )
        })
}

/// Expands assertions into the test body so the test-quality auditor sees them.
macro_rules! assert_same_file_heritage {
    ($parsed:expr, $source:expr, $target:expr, $kind:expr) => {{
        let parsed: &ParseResult = $parsed;
        let source: &str = $source;
        let target: &str = $target;
        let kind: HeritageKind = $kind;
        let row = heritage_row(parsed, source, target);
        assert_eq!(row.heritage_kind, kind);
        assert_eq!(row.source_kind, CallTargetKind::Symbol);
        assert_eq!(
            row.source_symbol_id.as_deref(),
            Some(type_symbol_id(parsed, source).as_str())
        );
        assert_eq!(row.target_kind, CallTargetKind::Symbol);
        assert_eq!(
            row.target_symbol_id.as_deref(),
            Some(type_symbol_id(parsed, target).as_str())
        );
        assert!(
            row.source_external_module.is_none()
                || row.source_external_module.as_deref() == Some("")
        );
        assert!(
            row.target_external_module.is_none()
                || row.target_external_module.as_deref() == Some("")
        );
    }};
}

#[test]
fn python_subclass_resolves_same_file_base() {
    let parsed = parse_python(
        r#"
class Base:
    pass

class Derived(Base):
    pass
"#,
        &[],
    );
    assert_same_file_heritage!(&parsed, "Derived", "Base", HeritageKind::Inherits);
}

#[test]
fn typescript_extends_and_implements() {
    let parsed = parse_typescript(
        r#"
class B {}
interface I {}
class D extends B implements I {}
"#,
        &[],
    );
    assert_same_file_heritage!(&parsed, "D", "B", HeritageKind::Extends);
    assert_same_file_heritage!(&parsed, "D", "I", HeritageKind::Implements);
}

#[test]
fn rust_impl_and_supertrait() {
    let parsed = parse_rust(
        r#"
struct Thing;
trait Display {}
impl Display for Thing {}
trait Debug {}
trait Foo: Debug {}
"#,
        &[],
    );
    assert_same_file_heritage!(&parsed, "Thing", "Display", HeritageKind::Implements);
    assert_same_file_heritage!(&parsed, "Foo", "Debug", HeritageKind::Extends);
}

#[test]
fn go_embedding_only() {
    let parsed = parse_go(
        r#"
package app

type Bar struct {}

type Foo struct {
    Bar
    Named Bar
}

type IBar interface {
    Method()
}

type IFoo interface {
    IBar
}

type Satisfier struct {}

func (s Satisfier) Method() {}
"#,
        &[],
    );
    assert_same_file_heritage!(&parsed, "Foo", "Bar", HeritageKind::Extends);
    assert_same_file_heritage!(&parsed, "IFoo", "IBar", HeritageKind::Extends);
    assert!(
        parsed
            .inheritance
            .iter()
            .all(|row| row.source_name != "Satisfier"),
        "implicit interface satisfaction must not emit heritage: {:?}",
        parsed.inheritance
    );
    assert_eq!(
        parsed
            .inheritance
            .iter()
            .filter(|row| row.source_name == "Foo")
            .count(),
        1,
        "named struct fields must not emit extra heritage: {:?}",
        parsed.inheritance
    );
}

#[test]
fn all_emitting_languages_have_same_file_pair() {
    let python = parse_python(
        "class Base:\n    pass\nclass Derived(Base):\n    pass\n",
        &[],
    );
    assert_same_file_heritage!(&python, "Derived", "Base", HeritageKind::Inherits);

    let javascript = parse_javascript("class Base {}\nclass Derived extends Base {}\n", &[]);
    assert_same_file_heritage!(&javascript, "Derived", "Base", HeritageKind::Inherits);

    let typescript = parse_typescript("class Base {}\nclass Derived extends Base {}\n", &[]);
    assert_same_file_heritage!(&typescript, "Derived", "Base", HeritageKind::Extends);

    let go = parse_go(
        "package app\ntype Bar struct {}\ntype Foo struct { Bar }\n",
        &[],
    );
    assert_same_file_heritage!(&go, "Foo", "Bar", HeritageKind::Extends);

    let rust = parse_rust(
        "struct Base;\ntrait Trait {}\nimpl Trait for Base {}\n",
        &[],
    );
    assert_same_file_heritage!(&rust, "Base", "Trait", HeritageKind::Implements);

    let java = parse_java("class Base {}\nclass Derived extends Base {}\n", &[]);
    assert_same_file_heritage!(&java, "Derived", "Base", HeritageKind::Extends);

    let php = parse_php("<?php\nclass Base {}\nclass Derived extends Base {}\n", &[]);
    assert_same_file_heritage!(&php, "Derived", "Base", HeritageKind::Extends);

    let dart = parse_dart("class Base {}\nclass Derived extends Base {}\n", &[]);
    assert_same_file_heritage!(&dart, "Derived", "Base", HeritageKind::Extends);

    let csharp = parse_csharp("class Base {}\nclass Derived : Base {}\n", &[]);
    assert_same_file_heritage!(&csharp, "Derived", "Base", HeritageKind::Extends);

    let objc = parse_objc(
        "@interface Base\n@end\n@interface Derived : Base\n@end\n",
        &[],
    );
    assert_same_file_heritage!(&objc, "Derived", "Base", HeritageKind::Extends);

    let cpp = parse_source(
        "src/sample.cpp",
        "class Base {};\nclass Derived : public Base {};\n",
        &[],
    );
    assert_same_file_heritage!(&cpp, "Derived", "Base", HeritageKind::Extends);

    let ruby = parse_ruby("class Base\nend\nclass Derived < Base\nend\n", &[]);
    assert_same_file_heritage!(&ruby, "Derived", "Base", HeritageKind::Inherits);

    let kotlin = parse_kotlin("open class Base\nclass Derived : Base()\n", &[]);
    assert_same_file_heritage!(&kotlin, "Derived", "Base", HeritageKind::Extends);

    let scala = parse_scala("class Base\nclass Derived extends Base\n", &[]);
    assert_same_file_heritage!(&scala, "Derived", "Base", HeritageKind::Extends);

    let swift = parse_swift("class Base {}\nclass Derived: Base {}\n", &[]);
    assert_same_file_heritage!(&swift, "Derived", "Base", HeritageKind::Extends);
}

#[test]
fn rust_impl_source_resolves_across_files() {
    let parsed = parse_rust(
        r#"
use crate::types::{Drawable, Widget};

impl Drawable for Widget {}
"#,
        &[
            (
                "Cargo.toml",
                r#"[package]
name = "app"
"#,
            ),
            (
                "src/types.rs",
                "pub struct Widget {}\npub trait Drawable {}\n",
            ),
        ],
    );
    let row = heritage_row(&parsed, "Widget", "Drawable");
    assert_eq!(row.heritage_kind, HeritageKind::Implements);
    assert_eq!(row.source_kind, CallTargetKind::LocalImport);
    assert!(row.source_symbol_id.is_none());
    assert!(
        row.source_local_import_candidate_files()
            .iter()
            .any(|file| file == "src/types.rs"),
        "source candidates {:?}",
        row.source_local_import_candidate_files()
    );
    assert_eq!(row.target_kind, CallTargetKind::LocalImport);
    assert!(
        row.target_local_import_candidate_files()
            .iter()
            .any(|file| file == "src/types.rs"),
        "target candidates {:?}",
        row.target_local_import_candidate_files()
    );

    let same_file = parse_rust(
        r#"
struct Widget;
trait Drawable {}
impl Drawable for Widget {}
"#,
        &[],
    );
    assert_same_file_heritage!(&same_file, "Widget", "Drawable", HeritageKind::Implements);
}

#[test]
fn rust_impl_external_and_unresolved_sources() {
    let parsed = parse_rust(
        r#"
trait LocalTrait {}
impl LocalTrait for external_crate::ExternalType {}
impl LocalTrait for MissingType {}
"#,
        &[(
            "Cargo.toml",
            r#"[package]
name = "app"

[dependencies]
external_crate = "1"
"#,
        )],
    );
    let external = heritage_row(&parsed, "ExternalType", "LocalTrait");
    assert_eq!(external.heritage_kind, HeritageKind::Implements);
    assert_eq!(external.source_kind, CallTargetKind::External);
    assert_eq!(
        external.source_external_module.as_deref(),
        Some("external_crate")
    );
    assert!(external.source_symbol_id.is_none());
    assert!(external.source_local_import_candidate_files().is_empty());

    let unresolved = heritage_row(&parsed, "MissingType", "LocalTrait");
    assert_eq!(unresolved.heritage_kind, HeritageKind::Implements);
    assert_eq!(unresolved.source_kind, CallTargetKind::Unresolved);
    assert!(
        unresolved.source_external_module.is_none()
            || unresolved.source_external_module.as_deref() == Some("")
    );
    assert!(unresolved.source_local_import_candidate_files().is_empty());
}

#[test]
fn csharp_base_list_uses_inherits_when_kind_unproven() {
    let parsed = parse_csharp(
        r#"
interface IOther {}
interface IFoo : IOther {}
struct Payload : IFoo {}
class LocalClass {}
interface LocalIface {}
class Child : LocalClass {}
class Impl : LocalIface {}
class Mixed : UnresolvedBase, ImportedIface {}
"#,
        &[],
    );
    assert_same_file_heritage!(&parsed, "IFoo", "IOther", HeritageKind::Extends);
    assert_same_file_heritage!(&parsed, "Payload", "IFoo", HeritageKind::Implements);
    assert_same_file_heritage!(&parsed, "Child", "LocalClass", HeritageKind::Extends);
    assert_same_file_heritage!(&parsed, "Impl", "LocalIface", HeritageKind::Implements);
    let mixed_unresolved = heritage_row(&parsed, "Mixed", "UnresolvedBase");
    assert_eq!(mixed_unresolved.heritage_kind, HeritageKind::Inherits);
    assert_eq!(mixed_unresolved.target_kind, CallTargetKind::Unresolved);
    let mixed_imported = heritage_row(&parsed, "Mixed", "ImportedIface");
    assert_eq!(mixed_imported.heritage_kind, HeritageKind::Inherits);
}

#[test]
fn ruby_mixins_emit_implements() {
    let parsed = parse_ruby(
        r#"
class Base
end
module Mix
end
module Ext
end
module Pre
end
class Derived < Base
  include Mix
  extend Ext
  prepend Pre
end
"#,
        &[],
    );
    assert_same_file_heritage!(&parsed, "Derived", "Base", HeritageKind::Inherits);
    assert_same_file_heritage!(&parsed, "Derived", "Mix", HeritageKind::Implements);
    assert_same_file_heritage!(&parsed, "Derived", "Ext", HeritageKind::Implements);
    assert_same_file_heritage!(&parsed, "Derived", "Pre", HeritageKind::Implements);
}

#[test]
fn rust_inherent_impl_emits_nothing() {
    let parsed = parse_rust(
        r#"
struct Thing;
impl Thing {
    fn method(&self) {}
}
"#,
        &[],
    );
    assert!(
        parsed.inheritance.is_empty(),
        "inherent impl must not emit heritage: {:?}",
        parsed.inheritance
    );
}

#[test]
fn empty_query_languages_emit_no_rows() {
    let c = parse_source("src/sample.c", "struct Foo { int x; };\n", &[]);
    let elixir = parse_elixir("defmodule Foo do\nend\n", &[]);
    let lua = parse_lua("Foo = {}\nfunction Foo:bar() end\n", &[]);
    let bash = parse_source("scripts/main.sh", "foo() { :; }\n", &[]);
    let yaml = parse_source("config.yaml", "foo: bar\n", &[]);
    let json = parse_source("config.json", "{\"foo\": 1}\n", &[]);
    for (lang, parsed) in [
        ("c", &c),
        ("elixir", &elixir),
        ("lua", &lua),
        ("bash", &bash),
        ("yaml", &yaml),
        ("json", &json),
    ] {
        assert!(
            parsed.inheritance.is_empty(),
            "{lang} must emit no heritage rows: {:?}",
            parsed.inheritance
        );
    }
}

#[test]
fn heritage_external_and_unresolved_targets() {
    let parsed = parse_python(
        r#"
from collections.abc import Iterable

class Derived(Iterable):
    pass

class Other(UnboundBase):
    pass
"#,
        &[],
    );
    let external = heritage_row(&parsed, "Derived", "Iterable");
    assert_eq!(external.heritage_kind, HeritageKind::Inherits);
    assert_eq!(external.target_kind, CallTargetKind::External);
    assert_eq!(
        external.target_external_module.as_deref(),
        Some("collections.abc")
    );
    assert!(external.target_symbol_id.is_none());
    assert!(external.target_local_import_candidate_files().is_empty());

    let unresolved = heritage_row(&parsed, "Other", "UnboundBase");
    assert_eq!(unresolved.heritage_kind, HeritageKind::Inherits);
    assert_eq!(unresolved.target_kind, CallTargetKind::Unresolved);
    assert_eq!(unresolved.target_name, "UnboundBase");
    assert!(
        unresolved.target_external_module.is_none()
            || unresolved.target_external_module.as_deref() == Some("")
    );
    assert!(unresolved.target_local_import_candidate_files().is_empty());
}
