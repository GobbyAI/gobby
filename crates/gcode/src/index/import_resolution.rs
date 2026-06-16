mod context;
mod helpers;
mod js_local;
mod parser;
mod predicates;
mod rust_local;

#[cfg(test)]
mod tests;

pub(crate) const UNPARSED_IMPORT_PREFIX: &str = "UNPARSED:";

pub(crate) use context::{ExtractedImports, ImportBindings};
pub use context::{
    ImportResolutionContext, build_import_resolution_context,
    build_import_resolution_context_with_overrides,
};
pub(crate) use parser::{
    parse_import_statement, resolve_csharp_local_member_callee, resolve_external_callee,
    resolve_local_callee, resolve_local_member_callee, resolve_php_local_member_callee,
    resolve_ruby_local_member_callee, resolve_rust_local_qualified_callee,
    resolve_swift_local_callee, seed_import_bindings,
};
