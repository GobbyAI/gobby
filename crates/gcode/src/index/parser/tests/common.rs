use std::fs;
use std::path::{Path, PathBuf};

use tempfile::TempDir;

use crate::index::{languages, walker};
use crate::models::ParseResult;

use super::super::{build_import_resolution_context, parse_file_with_semantic};

pub(super) fn parse_source(
    file_name: &str,
    source: &str,
    extra_files: &[(&str, &str)],
) -> ParseResult {
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

pub(super) fn parse_python(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
    parse_source("sample.py", source, extra_files)
}

pub(super) fn parse_javascript(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
    parse_source("src/sample.js", source, extra_files)
}

pub(super) fn parse_typescript(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
    parse_source("src/sample.ts", source, extra_files)
}

pub(super) fn parse_go(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
    parse_source("cmd/sample.go", source, extra_files)
}

pub(super) fn parse_rust(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
    parse_source("src/main.rs", source, extra_files)
}

pub(super) fn parse_java(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
    parse_source("src/main/java/app/Sample.java", source, extra_files)
}

pub(super) fn parse_csharp(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
    parse_source("src/Sample.cs", source, extra_files)
}

pub(super) fn parse_php(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
    parse_source("src/sample.php", source, extra_files)
}

pub(super) fn parse_ruby(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
    parse_source("lib/sample.rb", source, extra_files)
}

pub(super) fn parse_dart(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
    parse_source("lib/sample.dart", source, extra_files)
}

pub(super) fn parse_elixir(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
    parse_source("lib/sample.ex", source, extra_files)
}

pub(super) fn parse_kotlin(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
    parse_source("src/main/kotlin/Sample.kt", source, extra_files)
}

pub(super) fn parse_swift(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
    parse_source("Sources/App/main.swift", source, extra_files)
}

pub(super) fn discover_supported_files(root: &Path) -> Vec<PathBuf> {
    let (candidates, _) = walker::discover_files(root, &[]);
    candidates
        .into_iter()
        .filter(|path| languages::detect_language(&path.to_string_lossy()).is_some())
        .collect()
}
