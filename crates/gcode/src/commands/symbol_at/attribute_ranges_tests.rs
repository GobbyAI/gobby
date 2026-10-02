use super::*;

fn check_declaration(path: &str, source: &str, name: &str, body: &str) -> anyhow::Result<()> {
    let directory = tempfile::tempdir()?;
    let root = directory.path();
    let path = root.join(path);
    std::fs::write(&path, source)?;
    let imports =
        crate::index::parser::build_import_resolution_context(root, std::slice::from_ref(&path));
    let parsed = crate::index::parser::parse_file_with_semantic(
        &path,
        "project",
        root,
        &[] as &[&str],
        &imports,
        None,
    )?
    .expect("supported fixture parses");
    let symbol = parsed
        .symbols
        .iter()
        .find(|symbol| symbol.name == name)
        .unwrap_or_else(|| panic!("missing {name} in {}: {:?}", path.display(), parsed.symbols));
    let start = source
        .find(body)
        .expect("expected declaration is in fixture");
    let end = start + body.len();
    assert_eq!(
        symbol_source(source.as_bytes(), symbol).0,
        body,
        "{}: {name}",
        path.display()
    );
    let by_id = crate::commands::symbols::read_symbol_source(root, symbol)?
        .expect("symbol-by-ID source exists");
    assert_eq!(by_id.text, body);
    assert_eq!(by_id.symbol_bytes, body.len());
    assert_eq!((symbol.byte_start, symbol.byte_end), (start, end));
    let first_line = source[..start]
        .bytes()
        .filter(|byte| *byte == b'\n')
        .count()
        + 1;
    let last_line = first_line + body.bytes().filter(|byte| *byte == b'\n').count();
    assert_eq!(
        (symbol.line_start, symbol.line_end),
        (first_line, last_line)
    );
    assert_eq!(
        symbol.content_hash,
        crate::index::hasher::symbol_content_hash(source.as_bytes(), start, end)?
    );
    assert_eq!(
        symbol.id,
        Symbol::make_id(
            "project",
            &symbol.file_path,
            &symbol.file_content_hash,
            name,
            &symbol.kind,
            start
        )
    );
    // Both line and byte selection must resolve the metadata to this symbol,
    // including when an enclosing class would otherwise contain the line.
    for target in [
        SymbolAtTarget {
            line: first_line,
            byte_offset: None,
        },
        SymbolAtTarget {
            line: first_line,
            byte_offset: Some(start),
        },
    ] {
        let selected = select_symbol(&parsed.symbols, target).expect("declaration selected");
        assert_eq!(selected.symbol.id, symbol.id);
        assert_eq!(selected.match_kind, MatchKind::Containing);
    }
    Ok(())
}

#[test]
fn attribute_ranges_python_stacked_decorators() -> anyhow::Result<()> {
    check_declaration(
        "example.py",
        "# preceding comment\n@first\n@second(\n    enabled=True,\n)\nasync def work():\n    return 1\n\ndef after():\n    pass\n",
        "work",
        "@first\n@second(\n    enabled=True,\n)\nasync def work():\n    return 1",
    )?;
    Ok(())
}

#[test]
fn attribute_ranges_python_autouse_fixture() -> anyhow::Result<()> {
    check_declaration(
        "conftest.py",
        "import pytest\n\n@pytest.fixture(autouse=True)\ndef clear_identity():\n    yield\n",
        "clear_identity",
        "@pytest.fixture(autouse=True)\ndef clear_identity():\n    yield",
    )?;
    Ok(())
}

#[test]
fn attribute_ranges_python_class() -> anyhow::Result<()> {
    check_declaration(
        "example.py",
        "@registered\n@dataclass(frozen=True)\nclass Record:\n    value: int\n",
        "Record",
        "@registered\n@dataclass(frozen=True)\nclass Record:\n    value: int",
    )?;
    Ok(())
}

#[test]
fn attribute_ranges_rust_test_and_cfg() -> anyhow::Result<()> {
    check_declaration(
        "example.rs",
        "fn before() {}\n\n#[cfg(\n    test\n)]\n// between attributes\n#[test]\nfn verifies() {}\nfn after() {}\n",
        "verifies",
        "#[cfg(\n    test\n)]\n// between attributes\n#[test]\nfn verifies() {}",
    )?;
    Ok(())
}

#[test]
fn attribute_ranges_rust_derive() -> anyhow::Result<()> {
    check_declaration(
        "example.rs",
        "#![allow(dead_code)]\n#[repr(C)]\n#[derive(Debug)]\nstruct Record { value: u32 }\n",
        "Record",
        "#[repr(C)]\n#[derive(Debug)]\nstruct Record { value: u32 }",
    )?;
    Ok(())
}

#[test]
fn attribute_ranges_typescript_method() -> anyhow::Result<()> {
    check_declaration(
        "example.ts",
        "class Controller {\n  @first\n  @route(\n    '/item'\n  )\n  fetch() { return 1; }\n  after() {}\n}\n",
        "fetch",
        "@first\n  @route(\n    '/item'\n  )\n  fetch() { return 1; }",
    )?;
    Ok(())
}

#[test]
fn attribute_ranges_typescript_exported_class() -> anyhow::Result<()> {
    check_declaration(
        "example.tsx",
        "@registered\n@sealed\nexport class Controller { render() { return <div/>; } }\n",
        "Controller",
        "@registered\n@sealed\nexport class Controller { render() { return <div/>; } }",
    )?;
    Ok(())
}

#[test]
fn attribute_ranges_javascript_exported_class() -> anyhow::Result<()> {
    check_declaration(
        "example.js",
        "@registered\nexport class Controller { run() {} }\n",
        "Controller",
        "@registered\nexport class Controller { run() {} }",
    )?;
    Ok(())
}

#[test]
fn attribute_ranges_dart_method() -> anyhow::Result<()> {
    check_declaration(
        "example.dart",
        "class Controller {\n  @override\n  void run() {}\n}\n",
        "run",
        "@override\n  void run() {}",
    )?;
    Ok(())
}

#[test]
fn attribute_ranges_elixir_module_attributes() -> anyhow::Result<()> {
    check_declaration(
        "example.ex",
        "defmodule Example do\n  @doc \"Runs\"\n  @spec run() :: atom()\n  def run(), do: :ok\nend\n",
        "run",
        "@doc \"Runs\"\n  @spec run() :: atom()\n  def run(), do: :ok",
    )?;
    Ok(())
}

#[test]
fn attribute_ranges_attributes_already_inside_declarations() -> anyhow::Result<()> {
    for (path, source, name) in [
        ("Example.java", "@Deprecated\nclass Example {}", "Example"),
        ("Example.cs", "[Obsolete]\nclass Example {}", "Example"),
        (
            "example.php",
            "<?php\n#[Route('/')]\nfunction run() {}",
            "run",
        ),
        ("example.kt", "@Deprecated(\"old\")\nfun run() {}", "run"),
        (
            "example.scala",
            "@deprecated(\"old\", \"1\")\ndef run(): Unit = {}",
            "run",
        ),
        ("example.swift", "@MainActor\nfunc run() {}", "run"),
        (
            "example.c",
            "__attribute__((noinline))\nvoid run() {}",
            "run",
        ),
        (
            "example.cpp",
            "[[nodiscard]]\nint run() { return 1; }",
            "run",
        ),
        (
            "example.m",
            "__attribute__((noinline))\nvoid run() {}",
            "run",
        ),
        ("example.dart", "@deprecated\nvoid run() {}", "run"),
        ("example.js", "@registered\nclass Example {}", "Example"),
    ] {
        let body = source.strip_prefix("<?php\n").unwrap_or(source);
        check_declaration(path, source, name, body)?;
    }
    Ok(())
}

#[test]
fn attribute_ranges_cpp_attributed_type() -> anyhow::Result<()> {
    check_declaration(
        "example.cpp",
        "[[deprecated]]\nstruct Record { int value; };\n",
        "Record",
        "[[deprecated]]\nstruct Record { int value; }",
    )?;
    Ok(())
}

#[test]
fn attribute_ranges_metadata_does_not_cross_declarations() -> anyhow::Result<()> {
    for (path, source, name, body) in [
        (
            "example.rs",
            "#[test]\nfn before() {}\n// separate item\nfn after() {}",
            "after",
            "fn after() {}",
        ),
        (
            "example.rs",
            "#![allow(dead_code)]\nfn after() {}",
            "after",
            "fn after() {}",
        ),
        (
            "example.ts",
            "class Example {\n @first\n before() {}\n after(@inject value: object) {}\n}",
            "after",
            "after(@inject value: object) {}",
        ),
        (
            "example.py",
            "@first\ndef before():\n    pass\n\ndef after():\n    pass",
            "after",
            "def after():\n    pass",
        ),
        (
            "example.ex",
            "@doc \"Before\"\ndef before(), do: :ok\ndef after(), do: :ok",
            "after",
            "def after(), do: :ok",
        ),
    ] {
        check_declaration(path, source, name, body)?;
    }
    Ok(())
}
