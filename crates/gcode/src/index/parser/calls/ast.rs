use streaming_iterator::StreamingIterator;
use tree_sitter::{Query, QueryCursor};

use anyhow::Context as _;

use crate::index::languages;
use crate::index::semantic::SemanticCallResolver;
use crate::models::CallRelation;

use super::resolution::{
    CallSyntaxKind, call_qualifier_path, call_syntax_kind, member_qualifier_path,
    split_qualified_callee,
};
use super::text::should_ignore_call_name;
use super::{CallExtractionContext, CallSite, materialize_call};

pub(super) fn extract_ast_calls(
    tree: &tree_sitter::Tree,
    source: &[u8],
    spec: &languages::LanguageSpec,
    ctx: CallExtractionContext<'_>,
    mut semantic_resolver: Option<&mut (dyn SemanticCallResolver + '_)>,
) -> anyhow::Result<Vec<CallRelation>> {
    let language = ctx.language;
    if spec.call_query.trim().is_empty() {
        return Ok(Vec::new());
    }

    let query = Query::new(ctx.ts_lang, spec.call_query).with_context(|| {
        format!(
            "failed to compile call query for language `{language}` while parsing {}",
            ctx.file_path.display()
        )
    })?;

    let mut cursor = QueryCursor::new();
    let mut matches = cursor.matches(&query, tree.root_node(), source);
    let capture_names = query.capture_names();
    let name_capture = capture_names.iter().position(|name| *name == "name");
    let call_capture = capture_names.iter().position(|name| *name == "call");
    let mut calls = Vec::new();

    while let Some(m) = matches.next() {
        let mut name_node = None;
        let mut call_node = None;

        for cap in m.captures {
            let capture_index = cap.index as usize;
            if name_capture == Some(capture_index) {
                name_node = Some(cap.node);
            } else if call_capture == Some(capture_index) {
                call_node = Some(cap.node);
            }
        }

        let name_n = match name_node {
            Some(n) => n,
            None => continue,
        };

        let raw_callee =
            String::from_utf8_lossy(&source[name_n.start_byte()..name_n.end_byte()]).to_string();
        let (callee_name, qualifier_from_name) = split_qualified_callee(&raw_callee);
        if should_ignore_call_name(language, &callee_name) {
            continue;
        }

        let target = call_node.unwrap_or(name_n);
        // If the captured callee is already qualified, trust that text over a
        // prefix inferred from the wider call node.
        let qualifier_path = call_qualifier_path(qualifier_from_name, || {
            member_qualifier_path(source, target, name_n)
        });
        let detected_syntax = call_syntax_kind(name_n, target);
        let syntax = if detected_syntax == CallSyntaxKind::Bare && qualifier_path.is_some() {
            CallSyntaxKind::Member
        } else {
            detected_syntax
        };
        calls.push(materialize_call(
            source,
            &ctx,
            CallSite {
                callee_name,
                qualifier_path,
                name_byte: name_n.start_byte(),
                scope_byte: target.start_byte(),
                line: name_n.start_position().row + 1,
                syntax,
            },
            semantic_resolver.as_deref_mut(),
        )?);
    }

    Ok(calls)
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use crate::index::import_resolution::{
        ExtractedImports, ImportBindings, ImportResolutionContext, parse_import_statement,
    };
    use crate::models::CallTargetKind;

    use super::*;

    fn extract_js_calls(
        source: &str,
        query: &'static str,
        language: &str,
        import_bindings: &ImportBindings,
    ) -> Vec<CallRelation> {
        let ts_lang = languages::get_ts_language("javascript").expect("javascript language");
        let mut parser = tree_sitter::Parser::new();
        parser
            .set_language(&ts_lang)
            .expect("set javascript language");
        let tree = parser.parse(source, None).expect("parse javascript");
        let spec = languages::LanguageSpec {
            extensions: &[".js"],
            symbol_query: "",
            import_query: "",
            call_query: query,
        };
        let import_context = ImportResolutionContext::default();
        let ctx = CallExtractionContext {
            language,
            ts_lang: &ts_lang,
            rel_path: "src/app.js",
            symbols: &[],
            import_context: &import_context,
            import_bindings,
            file_path: Path::new("src/app.js"),
            root_path: Path::new("."),
        };

        extract_ast_calls(&tree, source.as_bytes(), &spec, ctx, None).expect("extract calls")
    }

    fn js_bindings(import_text: &str) -> ImportBindings {
        let import_context = ImportResolutionContext::default();
        let mut extracted = ExtractedImports::default();
        parse_import_statement(
            "javascript",
            import_text,
            "src/app.js",
            &import_context,
            &mut extracted,
        );
        extracted.bindings
    }

    #[test]
    fn skips_matches_without_name_capture() {
        let calls = extract_js_calls(
            "work();",
            "(call_expression function: (identifier) @call)",
            "javascript",
            &ImportBindings::default(),
        );

        assert!(calls.is_empty());
    }

    #[test]
    fn ignores_qualified_keyword_callee_after_split() {
        let calls = extract_js_calls(
            "obj.await();",
            "(call_expression function: (member_expression) @name) @call",
            "dart",
            &ImportBindings::default(),
        );

        assert!(calls.is_empty());
    }

    #[test]
    fn member_call_uses_qualifier_path_from_call_node() {
        let bindings = js_bindings("import fs from 'fs';");
        let calls = extract_js_calls(
            "import fs from 'fs';\nfs.readFile();",
            "(call_expression function: (member_expression property: (property_identifier) @name)) @call",
            "javascript",
            &bindings,
        );

        assert_eq!(calls.len(), 1);
        let call = &calls[0];
        assert_eq!(call.callee_name, "readFile");
        assert_eq!(call.line, 2);
        assert_eq!(call.callee_target_kind, CallTargetKind::External);
        assert_eq!(call.callee_external_module.as_deref(), Some("fs"));
    }

    #[test]
    fn bare_detected_syntax_upgrades_to_member_when_qualified_name_is_captured() {
        let bindings = js_bindings("import fs from 'fs';");
        let calls = extract_js_calls(
            "import fs from 'fs';\nfs.readFile();",
            "(call_expression function: (member_expression) @name) @call",
            "javascript",
            &bindings,
        );

        assert_eq!(calls.len(), 1);
        let call = &calls[0];
        assert_eq!(call.callee_name, "readFile");
        assert_eq!(call.callee_target_kind, CallTargetKind::External);
        assert_eq!(call.callee_external_module.as_deref(), Some("fs"));
    }
}
