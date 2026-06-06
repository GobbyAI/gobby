use crate::models::ImportRelation;

use super::super::context::{ExternalRootBinding, ExtractedImports, ImportResolutionContext};
use super::super::helpers::{
    collapse_whitespace, dart_import_alias, elixir_alias_as, extract_quoted_string,
    is_elixir_alias_path,
};
use super::super::predicates::is_external_dart_uri;

pub(crate) fn parse_swift_import_statement(
    text: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    let normalized = text.trim();
    let Some(rest) = normalized.strip_prefix("import ") else {
        return;
    };

    let mut tokens = rest.split_whitespace();
    let mut module_token = tokens.next().unwrap_or_default();
    if matches!(
        module_token,
        "class" | "struct" | "enum" | "protocol" | "func" | "typealias" | "var" | "let"
    ) {
        module_token = tokens.next().unwrap_or_default();
    }
    if module_token.is_empty() {
        return;
    }
    let module = module_token.split('.').next().unwrap_or_default();
    extracted.imports.push(ImportRelation {
        file_path: rel_path.to_string(),
        module_name: rest.to_string(),
    });
    if module.is_empty()
        || matches!(
            module,
            "class" | "struct" | "enum" | "protocol" | "func" | "typealias" | "var" | "let"
        )
        || import_context.swift_local_modules.contains(module)
    {
        return;
    }

    extracted.bindings.external_roots.insert(
        module.to_string(),
        ExternalRootBinding {
            module: module.to_string(),
            module_from_qualifier: false,
        },
    );
}

pub(crate) fn parse_ruby_import_statement(
    text: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    let normalized = text.trim();
    let Some(method) = normalized.split_whitespace().next() else {
        return;
    };

    let literal = extract_quoted_string(normalized);
    extracted.imports.push(ImportRelation {
        file_path: rel_path.to_string(),
        module_name: literal.clone().unwrap_or_else(|| normalized.to_string()),
    });

    if method != "require" {
        return;
    }
    let Some(required) = literal else {
        return;
    };
    let Some(root) = import_context.ruby_require_root(&required) else {
        return;
    };
    if import_context.ruby_local_constant_roots.contains(root) {
        return;
    }
    extracted.bindings.external_roots.insert(
        root.to_string(),
        ExternalRootBinding {
            module: required,
            module_from_qualifier: false,
        },
    );
}

pub(crate) fn parse_dart_import_statement(
    text: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    let normalized = collapse_whitespace(text);
    if !normalized.starts_with("import ") {
        return;
    }
    let Some(uri) = extract_quoted_string(&normalized) else {
        return;
    };

    extracted.imports.push(ImportRelation {
        file_path: rel_path.to_string(),
        module_name: uri.clone(),
    });

    if !is_external_dart_uri(&uri, import_context) {
        return;
    }
    if let Some(alias) = dart_import_alias(&normalized) {
        extracted.bindings.member.insert(alias, uri);
    } else {
        extracted.bindings.bare_wildcard_modules.push(uri);
    }
}

pub(crate) fn parse_elixir_import_statement(
    text: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    let normalized = collapse_whitespace(text);
    let Some((keyword, rest)) = normalized.split_once(' ') else {
        return;
    };
    if !matches!(keyword, "alias" | "import" | "require") {
        return;
    }
    let target = rest.split([',', ' ']).next().unwrap_or_default().trim();
    extracted.imports.push(ImportRelation {
        file_path: rel_path.to_string(),
        module_name: if target.is_empty() {
            normalized.clone()
        } else {
            target.to_string()
        },
    });

    if !is_elixir_alias_path(target) {
        return;
    }
    let Some(root) = target.split('.').next() else {
        return;
    };
    if import_context.elixir_local_module_roots.contains(root) {
        return;
    }
    let Some(module) = import_context.elixir_external_root_module(root) else {
        return;
    };

    if keyword == "alias" {
        let alias = elixir_alias_as(&normalized)
            .unwrap_or_else(|| target.rsplit('.').next().unwrap_or(target).to_string());
        extracted.bindings.member.insert(alias, target.to_string());
    } else if keyword == "import" {
        extracted
            .bindings
            .bare_wildcard_modules
            .push(target.to_string());
    }
    extracted.bindings.external_roots.insert(
        root.to_string(),
        ExternalRootBinding {
            module: module.to_string(),
            module_from_qualifier: true,
        },
    );
}
