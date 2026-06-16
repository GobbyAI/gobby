use crate::models::ImportRelation;

use super::super::context::{
    ExternalImportBinding, ExternalRootBinding, ExtractedImports, ImportResolutionContext,
    LocalCallBinding,
};
use super::super::predicates::{is_external_csharp_path, is_external_java_class};

pub(crate) fn parse_java_import_statement(
    text: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    let normalized = text.trim().trim_end_matches(';').trim();
    let Some(rest) = normalized.strip_prefix("import ") else {
        return;
    };

    let (is_static, target) = rest
        .strip_prefix("static ")
        .map(|target| (true, target.trim()))
        .unwrap_or((false, rest.trim()));
    extracted.imports.push(ImportRelation {
        file_path: rel_path.to_string(),
        module_name: target.to_string(),
    });

    if target.ends_with(".*") {
        return;
    }

    if is_static {
        let Some((class_path, member_name)) = target.rsplit_once('.') else {
            return;
        };
        if is_external_java_class(class_path, import_context) {
            extracted.bindings.bare.insert(
                member_name.to_string(),
                ExternalImportBinding {
                    module: class_path.to_string(),
                    callee_name: member_name.to_string(),
                },
            );
            return;
        }
        // Local static import: the member is invoked bare (`member()`); bind it
        // to the declaring class's file(s) so the post-write DB pass narrows it
        // to the real method symbol (or degrades to unresolved).
        let candidate_files = import_context.java_candidate_files(class_path);
        if candidate_files.is_empty() {
            return;
        }
        extracted.bindings.bare.remove(member_name);
        extracted.bindings.local_bare.insert(
            member_name.to_string(),
            LocalCallBinding {
                candidate_files,
                callee_name: member_name.to_string(),
            },
        );
        return;
    }

    let class_alias = target
        .rsplit('.')
        .next()
        .expect("rsplit always yields at least one segment");
    if is_external_java_class(target, import_context) {
        extracted
            .bindings
            .member
            .insert(class_alias.to_string(), target.to_string());
        return;
    }
    // Local single-type import: bind the class alias for both static member
    // calls (`Class.m()` -> member channel, resolves the method) and
    // constructor calls (`new Class()` -> bare channel, resolves the class).
    // The post-write DB pass narrows the candidate file(s) to a real id.
    let candidate_files = import_context.java_candidate_files(target);
    if candidate_files.is_empty() {
        return;
    }
    extracted.bindings.bare.remove(class_alias);
    extracted.bindings.member.remove(class_alias);
    extracted
        .bindings
        .local_member
        .insert(class_alias.to_string(), candidate_files.clone());
    extracted.bindings.local_bare.insert(
        class_alias.to_string(),
        LocalCallBinding {
            candidate_files,
            callee_name: class_alias.to_string(),
        },
    );
}

pub(crate) fn parse_csharp_import_statement(
    text: &str,
    rel_path: &str,
    import_context: &ImportResolutionContext,
    extracted: &mut ExtractedImports,
) {
    let normalized = text.trim().trim_end_matches(';').trim();
    let Some(rest) = normalized.strip_prefix("using ") else {
        return;
    };

    if let Some(target) = rest.strip_prefix("static ") {
        let target = normalize_csharp_global_alias(target.trim());
        extracted.imports.push(ImportRelation {
            file_path: rel_path.to_string(),
            module_name: target.clone(),
        });
        if is_external_csharp_path(&target, import_context) {
            extracted.bindings.bare_wildcard_modules.push(target);
        }
        return;
    }

    if let Some((alias, target)) = rest.split_once('=') {
        let alias = alias.trim();
        let target = normalize_csharp_global_alias(target.trim());
        extracted.imports.push(ImportRelation {
            file_path: rel_path.to_string(),
            module_name: target.clone(),
        });
        // Empty aliases come from malformed `using = ...` declarations and are ignored.
        if alias.is_empty() {
            return;
        }
        if is_external_csharp_path(&target, import_context) {
            extracted.bindings.member.insert(alias.to_string(), target);
            return;
        }
        // Local type alias: `using X = Ns.Type;` then `X.M()` resolves the
        // method against the aliased type's file(s) via the member channel. The
        // post-write DB pass narrows the candidate file(s) to a real id.
        let candidate_files = import_context.csharp_type_files(&target);
        if candidate_files.is_empty() {
            return;
        }
        extracted.bindings.member.remove(alias);
        extracted
            .bindings
            .local_member
            .insert(alias.to_string(), candidate_files);
        return;
    }

    let namespace = normalize_csharp_global_alias(rest.trim());
    extracted.imports.push(ImportRelation {
        file_path: rel_path.to_string(),
        module_name: namespace.clone(),
    });
    if !is_external_csharp_path(&namespace, import_context) {
        // Local namespace import: `using Ns;` brings its types into scope, so a
        // simple-type member call `Type.M()` resolves `Type` against the
        // declared `Ns.Type` file(s) in `resolve_csharp_local_member_callee`.
        extracted.bindings.csharp_local_namespaces.push(namespace);
        return;
    }
    if let Some(root) = namespace.split('.').next()
        && !root.is_empty()
    {
        extracted.bindings.external_roots.insert(
            root.to_string(),
            ExternalRootBinding {
                module: root.to_string(),
                module_from_qualifier: true,
            },
        );
    }
}

fn normalize_csharp_global_alias(path: &str) -> String {
    path.strip_prefix("global::").unwrap_or(path).to_string()
}

pub(crate) fn csharp_global_qualifier_parts<'a>(
    root_alias: &'a str,
    qualifier_path: &'a str,
) -> Option<(&'a str, &'a str)> {
    if root_alias != "global" {
        return None;
    }
    let qualifier_path = qualifier_path.strip_prefix("global::")?;
    let root_alias = qualifier_path
        .split('.')
        .next()
        .filter(|root| !root.is_empty())?;
    Some((root_alias, qualifier_path))
}
