use crate::config::Context;
use crate::vector::code_symbols::{
    self, CodeSymbolVectorLifecycleAction, CodeSymbolVectorLifecycleStatus,
};

pub fn lifecycle_status(
    ctx: &Context,
    action: CodeSymbolVectorLifecycleAction,
) -> CodeSymbolVectorLifecycleStatus {
    let prefix = ctx
        .qdrant
        .as_ref()
        .map(|config| config.collection_prefix.as_str())
        .unwrap_or("code_symbols_");
    code_symbols::lifecycle_status(ctx.project_id.clone(), prefix, action)
}
