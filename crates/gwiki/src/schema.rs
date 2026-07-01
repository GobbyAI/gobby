use gobby_core::setup::{
    AttachedValidator, Guidance, RequiredObject, SetupIssue, StoreKind, ValidationContext,
    ValidationReport,
};

use crate::setup::{GWIKI_POSTGRES_INDEXES, GWIKI_POSTGRES_TABLES};

#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
pub const MIGRATION_HINT: &str = "Run Gobby hub migrations, then `gwiki setup` to validate gwiki-owned PostgreSQL tables and indexes.";
const DEFAULT_SCHEMA: &str = "public";

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
            action: "run Gobby hub migrations, then validate with gwiki setup before runtime wiki commands".to_string(),
            command_hint: Some("gwiki setup".to_string()),
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use gobby_core::setup::ValidationContext;

    #[test]
    fn missing_schema_requires_explicit_setup() {
        let mut ctx = ValidationContext {
            pg: None,
            falkor_config: None,
            qdrant_config: None,
        };

        let report = GwikiRuntimeSchema.validate(&mut ctx);

        assert!(
            !report.is_healthy(),
            "missing gwiki schema must fail validation"
        );
        assert_eq!(
            report.missing.len(),
            GWIKI_POSTGRES_TABLES.len() + GWIKI_POSTGRES_INDEXES.len()
        );
        assert!(
            report
                .missing
                .iter()
                .all(|(name, issue)| name.starts_with("gwiki_")
                    && issue.guidance.command_hint.as_deref() == Some("gwiki setup")),
            "missing schema issues must point at explicit gwiki setup: {:?}",
            report.missing
        );
        assert!(MIGRATION_HINT.contains("gwiki setup"));
        let source = include_str!("schema.rs")
            .split("#[cfg(test)]")
            .next()
            .expect("implementation source");
        assert!(!source.contains("CREATE TABLE"));
        assert!(!source.contains("CREATE INDEX"));
        assert!(!source.contains("ALTER TABLE"));
        assert!(!source.contains("DROP TABLE"));
    }

    #[test]
    fn relation_validation_qualifies_public_schema() {
        assert_eq!(
            relation_regclass_name("gwiki_documents"),
            "public.gwiki_documents"
        );
    }
}
