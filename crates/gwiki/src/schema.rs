use gobby_core::degradation::{Guidance, SetupIssue};
use gobby_core::schema::{
    AttachedValidator, RequiredObject, StoreKind, ValidationContext, ValidationReport,
};

const DEFAULT_SCHEMA: &str = "public";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GwikiTable {
    Documents,
    Chunks,
    Links,
    Sources,
    Ingestions,
}

impl GwikiTable {
    pub fn name(self) -> &'static str {
        match self {
            Self::Documents => "gwiki_documents",
            Self::Chunks => "gwiki_chunks",
            Self::Links => "gwiki_links",
            Self::Sources => "gwiki_sources",
            Self::Ingestions => "gwiki_ingestions",
        }
    }
}

pub const GWIKI_POSTGRES_TABLES: &[GwikiTable] = &[
    GwikiTable::Documents,
    GwikiTable::Chunks,
    GwikiTable::Links,
    GwikiTable::Sources,
    GwikiTable::Ingestions,
];

pub const GWIKI_POSTGRES_INDEXES: &[&str] = &[
    "gwiki_documents_scope_path_idx",
    "gwiki_documents_content_hash_idx",
    "gwiki_chunks_scope_path_idx",
    "gwiki_sources_scope_path_idx",
    "gwiki_links_scope_idx",
    "gwiki_documents_search_bm25",
    "gwiki_chunks_search_bm25",
];

#[derive(Debug, Default)]
pub struct GwikiRuntimeSchema;

impl AttachedValidator for GwikiRuntimeSchema {
    fn required_objects(&self) -> Vec<RequiredObject> {
        GWIKI_POSTGRES_TABLES
            .iter()
            .map(|table| table.name())
            .chain(GWIKI_POSTGRES_INDEXES.iter().copied())
            .map(required_relation)
            .collect()
    }
}

pub fn validate_runtime_schema(ctx: &mut ValidationContext<'_>) -> ValidationReport {
    GwikiRuntimeSchema.validate(ctx)
}

fn required_relation(relation: &'static str) -> RequiredObject {
    RequiredObject {
        name: relation.to_string(),
        store: StoreKind::Postgres,
        validator: Box::new(move |ctx| validate_relation(ctx, relation)),
    }
}

fn validate_relation(ctx: &mut ValidationContext<'_>, relation: &str) -> Result<(), SetupIssue> {
    let Some(pg) = ctx.pg.as_deref_mut() else {
        return Err(missing_relation_issue(
            relation,
            "PostgreSQL connection was not supplied",
        ));
    };

    let qualified = relation_regclass_name(relation);
    let row = pg
        .query_one("SELECT to_regclass($1) IS NOT NULL", &[&qualified])
        .map_err(|err| missing_relation_issue(relation, &err.to_string()))?;
    let exists: bool = row.get(0);

    if exists {
        Ok(())
    } else {
        Err(missing_relation_issue(relation, "relation is missing"))
    }
}

fn relation_regclass_name(relation: &str) -> String {
    format!("{DEFAULT_SCHEMA}.{relation}")
}

fn missing_relation_issue(relation: &str, detail: &str) -> SetupIssue {
    SetupIssue {
        object_name: relation.to_string(),
        store: "postgres".to_string(),
        guidance: Guidance {
            problem: format!(
                "required gwiki datastore object `{relation}` is unavailable: {detail}"
            ),
            action: "run Gobby hub migrations, then retry the wiki command".to_string(),
            command_hint: Some("gdaemon apply".to_string()),
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use gobby_core::schema::ValidationContext;

    #[test]
    fn required_objects_cover_tables_and_indexes() {
        let objects = GwikiRuntimeSchema.required_objects();
        let names: Vec<_> = objects.iter().map(|object| object.name.as_str()).collect();
        assert!(names.contains(&"gwiki_documents"));
        assert!(names.contains(&"gwiki_documents_search_bm25"));
    }

    #[test]
    fn missing_connection_is_a_validation_issue() {
        let mut ctx = ValidationContext {
            pg: None,
            falkor_config: None,
            qdrant_config: None,
        };
        let report = validate_runtime_schema(&mut ctx);
        assert!(!report.is_healthy());
        assert!(
            report
                .missing
                .iter()
                .any(|(name, _)| name == "gwiki_documents")
        );
    }
}
