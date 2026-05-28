use crate::config::{CODE_SYMBOL_COLLECTION_PREFIX, Context};
use crate::vector::code_symbols::{
    self, CodeSymbolVectorLifecycleAction, CodeSymbolVectorLifecycleStatus,
};

pub fn lifecycle_status(
    ctx: &Context,
    action: CodeSymbolVectorLifecycleAction,
) -> CodeSymbolVectorLifecycleStatus {
    let prefix = CODE_SYMBOL_COLLECTION_PREFIX;
    code_symbols::lifecycle_status(ctx.project_id.clone(), prefix, action)
}
