use crate::config::{CODE_SYMBOL_COLLECTION_PREFIX, Context};
use crate::db;
use crate::output::{self, Format};
use crate::vector::code_symbols::{
    self, CodeSymbolVectorLifecycle, CodeSymbolVectorLifecycleAction,
    CodeSymbolVectorLifecycleOutput, CodeSymbolVectorLifecycleStatus, VectorLifecycleError,
};

pub fn lifecycle_status(
    ctx: &Context,
    action: CodeSymbolVectorLifecycleAction,
) -> CodeSymbolVectorLifecycleStatus {
    let prefix = CODE_SYMBOL_COLLECTION_PREFIX;
    code_symbols::lifecycle_status(ctx.project_id.clone(), prefix, action)
}

pub(crate) fn lifecycle_from_context(
    ctx: &Context,
) -> Result<CodeSymbolVectorLifecycle, VectorLifecycleError> {
    let qdrant = ctx
        .qdrant
        .clone()
        .ok_or(VectorLifecycleError::MissingQdrantConfig)?;
    let embedding = ctx
        .embedding
        .clone()
        .ok_or(VectorLifecycleError::MissingEmbeddingConfig)?;
    CodeSymbolVectorLifecycle::new(
        ctx.project_id.clone(),
        qdrant,
        embedding,
        ctx.code_vectors.clone(),
    )
}

pub fn sync_file(ctx: &Context, file_path: &str, format: Format) -> anyhow::Result<()> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let symbols = code_symbols::fetch_symbols_for_file(&mut conn, &ctx.project_id, file_path)?;
    let output = lifecycle_from_context(ctx)?.sync_file_symbols(file_path, &symbols)?;
    print_lifecycle_output(&output, format)
}

pub fn clear(ctx: &Context, format: Format) -> anyhow::Result<()> {
    let output = lifecycle_from_context(ctx)?.clear_project_vectors()?;
    print_lifecycle_output(&output, format)
}

pub fn rebuild(ctx: &Context, format: Format) -> anyhow::Result<()> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let symbols = code_symbols::fetch_symbols_for_project(&mut conn, &ctx.project_id)?;
    let output = lifecycle_from_context(ctx)?.rebuild_symbols(&symbols)?;
    print_lifecycle_output(&output, format)
}

fn print_lifecycle_output(
    output: &CodeSymbolVectorLifecycleOutput,
    format: Format,
) -> anyhow::Result<()> {
    match format {
        Format::Json => output::print_json(output),
        Format::Text => {
            eprintln!("{}", output.summary);
            output::print_json_compact(output)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn make_ctx() -> Context {
        Context {
            database_url: "postgresql://localhost/nonexistent".to_string(),
            project_root: PathBuf::from("/nonexistent"),
            project_id: "project-1".to_string(),
            quiet: true,
            falkordb: None,
            qdrant: None,
            embedding: None,
            code_vectors: crate::config::CodeVectorSettings { vector_dim: None },
            daemon_url: None,
        }
    }

    #[test]
    fn vector_lifecycle_requires_config() {
        let err = lifecycle_from_context(&make_ctx()).expect_err("missing config must fail");
        assert!(matches!(
            err,
            code_symbols::VectorLifecycleError::MissingQdrantConfig
        ));

        let ctx = Context {
            qdrant: Some(crate::config::QdrantConfig {
                url: Some("http://localhost:6333".to_string()),
                api_key: None,
            }),
            ..make_ctx()
        };
        let err = lifecycle_from_context(&ctx).expect_err("missing embedding must fail");
        assert!(matches!(
            err,
            code_symbols::VectorLifecycleError::MissingEmbeddingConfig
        ));
    }
}
