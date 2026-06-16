use std::path::Path;

use crate::index::import_resolution::{self, ImportBindings};
use crate::index::languages;
use crate::index::semantic::{
    SemanticCallRequest, SemanticCallResolver, SemanticCallTarget, SemanticTargetKind,
};
use crate::models::{CallRelation, Symbol};

use super::ImportResolutionContext;

mod ast;
mod dart_textual;
mod objc_ast;
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
    if ctx.language == "objc" {
        return objc_ast::extract_objc_calls(tree, source, spec, ctx, semantic_resolver);
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
    let lua_qualifier_path = if ctx.language == "lua"
        && (site.qualifier_path.is_none() || site.qualifier_path.as_deref() == Some("require"))
    {
        lua_require_qualifier_before_name(source, site.name_byte)
    } else {
        None
    };
    let qualifier_path = lua_qualifier_path
        .as_deref()
        .or(site.qualifier_path.as_deref());
    let local_target = resolution::resolve_same_file_callee_for_language(
        ctx.language,
        ctx.symbols,
        caller_symbol,
        &site.callee_name,
        qualifier_path,
        site.syntax,
    );
    let root_alias = qualifier_path
        .and_then(resolution::qualifier_root_alias)
        .map(ToOwned::to_owned);
    let lua_require_bound = ctx.language == "lua"
        && match site.syntax {
            CallSyntaxKind::Bare => ctx
                .import_bindings
                .local_bare
                .contains_key(&site.callee_name),
            CallSyntaxKind::Member => root_alias
                .as_deref()
                .is_some_and(|alias| ctx.import_bindings.local_member.contains_key(alias)),
            CallSyntaxKind::Other => false,
        };
    let external_shadowed = !lua_require_bound
        && shadowing::external_call_is_shadowed(
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
            qualifier_path,
            site.syntax == CallSyntaxKind::Bare,
        )
    };
    let local_qualified_target =
        if local_target.is_none() && external_target.is_none() && !external_shadowed {
            import_resolution::resolve_rust_local_qualified_callee(
                ctx.import_context,
                ctx.rel_path,
                &site.callee_name,
                qualifier_path,
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
    let csharp_member_target = if local_target.is_none()
        && external_target.is_none()
        && local_qualified_target.is_none()
        && local_member_target.is_none()
        && !external_shadowed
    {
        import_resolution::resolve_csharp_local_member_callee(
            ctx.import_context,
            ctx.import_bindings,
            ctx.symbols,
            &site.callee_name,
            root_alias.as_deref(),
            qualifier_path,
            site.syntax == CallSyntaxKind::Member,
        )
    } else {
        None
    };
    let ruby_member_target = if ctx.language == "ruby"
        && local_target.is_none()
        && external_target.is_none()
        && local_qualified_target.is_none()
        && local_member_target.is_none()
        && csharp_member_target.is_none()
        && !external_shadowed
    {
        import_resolution::resolve_ruby_local_member_callee(
            ctx.import_context,
            ctx.symbols,
            &site.callee_name,
            root_alias.as_deref(),
            site.syntax == CallSyntaxKind::Member,
        )
    } else {
        None
    };
    let php_member_target = if ctx.language == "php"
        && local_target.is_none()
        && external_target.is_none()
        && local_qualified_target.is_none()
        && local_member_target.is_none()
        && csharp_member_target.is_none()
        && ruby_member_target.is_none()
        && !external_shadowed
    {
        import_resolution::resolve_php_local_member_callee(
            ctx.import_context,
            &site.callee_name,
            qualifier_path,
            site.syntax == CallSyntaxKind::Member,
        )
    } else {
        None
    };
    let swift_local_target = if ctx.language == "swift"
        && local_target.is_none()
        && external_target.is_none()
        && local_qualified_target.is_none()
        && local_member_target.is_none()
        && csharp_member_target.is_none()
        && ruby_member_target.is_none()
        && php_member_target.is_none()
        && !external_shadowed
    {
        import_resolution::resolve_swift_local_callee(
            ctx.import_context,
            ctx.rel_path,
            &site.callee_name,
            site.syntax == CallSyntaxKind::Bare,
        )
    } else {
        None
    };
    let dart_local_target = if ctx.language == "dart"
        && local_target.is_none()
        && external_target.is_none()
        && local_qualified_target.is_none()
        && local_member_target.is_none()
        && csharp_member_target.is_none()
        && ruby_member_target.is_none()
        && php_member_target.is_none()
        && swift_local_target.is_none()
        && !external_shadowed
    {
        import_resolution::resolve_dart_local_callee(
            ctx.import_bindings,
            ctx.symbols,
            &site.callee_name,
            site.syntax == CallSyntaxKind::Bare,
        )
    } else {
        None
    };
    let elixir_local_target = if ctx.language == "elixir"
        && local_target.is_none()
        && external_target.is_none()
        && local_qualified_target.is_none()
        && local_member_target.is_none()
        && csharp_member_target.is_none()
        && ruby_member_target.is_none()
        && php_member_target.is_none()
        && swift_local_target.is_none()
        && dart_local_target.is_none()
        && !external_shadowed
    {
        import_resolution::resolve_elixir_local_callee(
            ctx.import_context,
            ctx.import_bindings,
            ctx.symbols,
            &site.callee_name,
            qualifier_path,
            site.syntax == CallSyntaxKind::Bare,
            site.syntax == CallSyntaxKind::Member,
        )
    } else {
        None
    };
    let lua_require_target = if ctx.language == "lua"
        && local_target.is_none()
        && external_target.is_none()
        && local_qualified_target.is_none()
        && local_member_target.is_none()
        && csharp_member_target.is_none()
        && ruby_member_target.is_none()
        && php_member_target.is_none()
        && swift_local_target.is_none()
        && dart_local_target.is_none()
        && elixir_local_target.is_none()
        && !external_shadowed
    {
        import_resolution::resolve_lua_require_member_callee(
            ctx.import_context,
            &site.callee_name,
            qualifier_path,
            site.syntax == CallSyntaxKind::Member || qualifier_path.is_some(),
        )
    } else {
        None
    };
    let local_import_target = if local_target.is_none()
        && external_target.is_none()
        && local_qualified_target.is_none()
        && local_member_target.is_none()
        && csharp_member_target.is_none()
        && ruby_member_target.is_none()
        && php_member_target.is_none()
        && swift_local_target.is_none()
        && dart_local_target.is_none()
        && elixir_local_target.is_none()
        && lua_require_target.is_none()
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
    let shell_local_target = if ctx.language == "bash"
        && local_target.is_none()
        && external_target.is_none()
        && local_qualified_target.is_none()
        && local_member_target.is_none()
        && csharp_member_target.is_none()
        && ruby_member_target.is_none()
        && php_member_target.is_none()
        && swift_local_target.is_none()
        && dart_local_target.is_none()
        && elixir_local_target.is_none()
        && lua_require_target.is_none()
        && local_import_target.is_none()
        && !external_shadowed
    {
        import_resolution::resolve_shell_local_callee(
            ctx.import_bindings,
            ctx.symbols,
            &site.callee_name,
            site.syntax == CallSyntaxKind::Bare,
        )
    } else {
        None
    };
    let objc_local_target = if ctx.language == "objc"
        && local_target.is_none()
        && external_target.is_none()
        && local_qualified_target.is_none()
        && local_member_target.is_none()
        && csharp_member_target.is_none()
        && ruby_member_target.is_none()
        && php_member_target.is_none()
        && swift_local_target.is_none()
        && dart_local_target.is_none()
        && elixir_local_target.is_none()
        && lua_require_target.is_none()
        && local_import_target.is_none()
        && shell_local_target.is_none()
        && !external_shadowed
    {
        import_resolution::resolve_objc_local_callee(
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
        && csharp_member_target.is_none()
        && ruby_member_target.is_none()
        && php_member_target.is_none()
        && swift_local_target.is_none()
        && dart_local_target.is_none()
        && elixir_local_target.is_none()
        && lua_require_target.is_none()
        && local_import_target.is_none()
        && shell_local_target.is_none()
        && objc_local_target.is_none()
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
        .or(csharp_member_target)
        .or(ruby_member_target)
        .or(php_member_target)
        .or(swift_local_target)
        .or(dart_local_target)
        .or(elixir_local_target)
        .or(lua_require_target)
        .or(local_import_target)
        .or(shell_local_target)
        .or(objc_local_target)
    {
        // Cross-file local import: record the original name plus the candidate
        // target files. The post-write pass resolves it against `code_symbols`
        // to a real indexed id (or degrades it to unresolved).
        call =
            call.with_local_import_target(local_binding.callee_name, local_binding.candidate_files);
    } else if let Some(external_target) = external_target {
        call = call.with_external_target(external_target.callee_name, external_target.module);
    } else if let Some(semantic_target) = semantic_target {
        // A C/C++ semantic resolution is either an external dependency module or
        // a cross-file local definition. The local case rides the same
        // candidate-file → post-write DB resolution path as import bindings, so
        // `callers`/`blast-radius` see the canonical symbol instead of nothing.
        let SemanticCallTarget { callee_name, kind } = semantic_target;
        call = match kind {
            SemanticTargetKind::External(module) => call.with_external_target(callee_name, module),
            SemanticTargetKind::LocalCandidate(candidate_file) => {
                call.with_local_import_target(callee_name, vec![candidate_file])
            }
        };
    }
    Ok(call)
}

fn lua_require_qualifier_before_name(source: &[u8], name_byte: usize) -> Option<String> {
    let name_byte = name_byte.min(source.len());
    let line_start = source[..name_byte]
        .iter()
        .rposition(|byte| *byte == b'\n')
        .map(|idx| idx + 1)
        .unwrap_or(0);
    let prefix = std::str::from_utf8(&source[line_start..name_byte])
        .ok()?
        .trim_end();
    if !prefix.ends_with(['.', ':']) {
        return None;
    }
    let qualifier = prefix.trim_end_matches(['.', ':']).trim_end();
    if qualifier.starts_with("require") {
        Some(qualifier.to_string())
    } else {
        None
    }
}
