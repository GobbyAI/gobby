use std::path::Path;

use crate::index::import_resolution::{self, ImportBindings};
use crate::index::languages;
use crate::index::semantic::{SemanticCallRequest, SemanticCallResolver};
use crate::models::{CallRelation, Symbol};

use super::ImportResolutionContext;

mod ast;
mod dart_textual;
mod resolution;
mod shadowing;
mod text;

use resolution::CallSyntaxKind;

#[cfg(test)]
pub(super) use resolution::{call_qualifier_path, split_qualified_callee};
#[cfg(test)]
pub(super) use text::line_terminator_len;

pub(super) struct CallExtractionContext<'a> {
    pub(super) language: &'a str,
    pub(super) ts_lang: &'a tree_sitter::Language,
    pub(super) rel_path: &'a str,
    pub(super) symbols: &'a [Symbol],
    pub(super) import_context: &'a ImportResolutionContext,
    pub(super) import_bindings: &'a ImportBindings,
    pub(super) file_path: &'a Path,
    pub(super) root_path: &'a Path,
}

#[derive(Debug)]
struct CallSite {
    callee_name: String,
    qualifier_path: Option<String>,
    name_byte: usize,
    scope_byte: usize,
    line: usize,
    syntax: CallSyntaxKind,
}

pub(super) fn extract_calls(
    tree: &tree_sitter::Tree,
    source: &[u8],
    spec: &languages::LanguageSpec,
    ctx: CallExtractionContext<'_>,
    semantic_resolver: Option<&mut (dyn SemanticCallResolver + '_)>,
) -> anyhow::Result<Vec<CallRelation>> {
    if ctx.language == "dart" {
        return dart_textual::extract_textual_dart_calls(source, ctx, semantic_resolver);
    }
    ast::extract_ast_calls(tree, source, spec, ctx, semantic_resolver)
}

fn materialize_call(
    source: &[u8],
    ctx: &CallExtractionContext<'_>,
    site: CallSite,
    semantic_resolver: Option<&mut (dyn SemanticCallResolver + '_)>,
) -> anyhow::Result<CallRelation> {
    let caller_symbol = resolution::enclosing_symbol(ctx.symbols, site.scope_byte);
    let caller_symbol_id = caller_symbol.map(|s| s.id.clone()).unwrap_or_default();
    let local_target = resolution::resolve_same_file_callee_for_language(
        ctx.language,
        ctx.symbols,
        caller_symbol,
        &site.callee_name,
        site.qualifier_path.as_deref(),
        site.syntax,
    );
    let root_alias = site
        .qualifier_path
        .as_deref()
        .and_then(resolution::qualifier_root_alias)
        .map(ToOwned::to_owned);
    let external_shadowed = shadowing::external_call_is_shadowed(
        source,
        caller_symbol,
        site.scope_byte,
        &site.callee_name,
        root_alias.as_deref(),
        site.syntax,
    );
    let external_target = if external_shadowed {
        None
    } else {
        import_resolution::resolve_external_callee(
            ctx.import_context,
            ctx.import_bindings,
            ctx.symbols,
            &site.callee_name,
            root_alias.as_deref(),
            site.qualifier_path.as_deref(),
            site.syntax == CallSyntaxKind::Bare,
        )
    };
    let local_qualified_target =
        if local_target.is_none() && external_target.is_none() && !external_shadowed {
            import_resolution::resolve_rust_local_qualified_callee(
                ctx.import_context,
                ctx.rel_path,
                &site.callee_name,
                site.qualifier_path.as_deref(),
                site.syntax == CallSyntaxKind::Member,
            )
        } else {
            None
        };
    let local_member_target = if local_target.is_none()
        && external_target.is_none()
        && local_qualified_target.is_none()
        && !external_shadowed
    {
        import_resolution::resolve_local_member_callee(
            ctx.import_bindings,
            ctx.symbols,
            &site.callee_name,
            root_alias.as_deref(),
            site.syntax == CallSyntaxKind::Member,
        )
    } else {
        None
    };
    let local_import_target = if local_target.is_none()
        && external_target.is_none()
        && local_qualified_target.is_none()
        && local_member_target.is_none()
        && !external_shadowed
    {
        import_resolution::resolve_local_callee(
            ctx.import_bindings,
            ctx.symbols,
            &site.callee_name,
            site.syntax == CallSyntaxKind::Bare,
        )
    } else {
        None
    };
    let semantic_target = if local_target.is_none()
        && external_target.is_none()
        && local_qualified_target.is_none()
        && local_member_target.is_none()
        && local_import_target.is_none()
        && !external_shadowed
    {
        if let Some(resolver) = semantic_resolver {
            resolver.resolve(&SemanticCallRequest {
                language: ctx.language,
                file_path: ctx.file_path,
                root_path: ctx.root_path,
                source,
                callee_name: &site.callee_name,
                line: site.line,
                column: text::utf16_column_at_byte(source, site.name_byte),
            })?
        } else {
            None
        }
    } else {
        None
    };

    let mut call = CallRelation::new(
        caller_symbol_id,
        site.callee_name.clone(),
        ctx.rel_path.to_string(),
        site.line,
    );
    if let Some(callee_symbol_id) = local_target {
        call = call.with_symbol_target(callee_symbol_id);
    } else if let Some(local_binding) = local_qualified_target
        .or(local_member_target)
        .or(local_import_target)
    {
        // Cross-file local import: record the original name plus the candidate
        // target files. The post-write pass resolves it against `code_symbols`
        // to a real indexed id (or degrades it to unresolved).
        call =
            call.with_local_import_target(local_binding.callee_name, local_binding.candidate_files);
    } else if let Some(external_target) = external_target {
        call = call.with_external_target(external_target.callee_name, external_target.module);
    } else if let Some(semantic_target) = semantic_target {
        call =
            call.with_external_target(semantic_target.callee_name, semantic_target.external_module);
    }
    Ok(call)
}
