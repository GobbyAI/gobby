//! Attached-mode validation of externally managed datastore objects.

use crate::degradation::SetupIssue;

/// Datastore kind for object classification.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StoreKind {
    /// PostgreSQL hub datastore.
    Postgres,
    /// FalkorDB graph datastore.
    FalkorDB,
    /// Qdrant vector datastore.
    Qdrant,
}

/// Context supplied to validation callbacks.
///
/// Contains nullable mutable connections to each datastore. Consumers use
/// whichever connection their validator needs; `None` represents diagnostic or
/// explicitly degraded paths where a handle was not supplied.
pub struct ValidationContext<'a> {
    /// PostgreSQL connection supplied by the caller.
    pub pg: Option<&'a mut postgres::Client>,
    /// FalkorDB connection configuration, when configured.
    pub falkor_config: Option<&'a crate::config::FalkorConfig>,
    /// Qdrant connection configuration, when configured.
    pub qdrant_config: Option<&'a crate::config::QdrantConfig>,
}

/// Result of running all attached-mode validators.
#[derive(Debug, Default)]
pub struct ValidationReport {
    /// Names of objects that passed validation.
    pub present: Vec<String>,
    /// Objects that failed validation, with structured issue details.
    pub missing: Vec<(String, SetupIssue)>,
}

impl ValidationReport {
    /// Returns true when every required object passed validation.
    pub fn is_healthy(&self) -> bool {
        self.missing.is_empty()
    }
}

/// Consumer-supplied validation callback for a required object.
pub type RequiredValidator =
    dyn for<'ctx> FnMut(&mut ValidationContext<'ctx>) -> Result<(), SetupIssue>;

/// Required object that a consumer crate declares for attached validation.
pub struct RequiredObject {
    /// Human-readable name, such as `symbols table` or `wiki_docs table`.
    pub name: String,
    /// Store kind that owns the object.
    pub store: StoreKind,
    /// Consumer-supplied check function.
    pub validator: Box<RequiredValidator>,
}

/// Attached-mode validation: check that externally managed resources exist.
///
/// Attached validation must never create, alter, or drop datastore schema.
pub trait AttachedValidator {
    /// Declare the objects this consumer requires.
    fn required_objects(&self) -> Vec<RequiredObject>;

    /// Run all validators and return a report of present and missing objects.
    fn validate(&self, ctx: &mut ValidationContext<'_>) -> ValidationReport {
        let mut report = ValidationReport::default();
        for mut obj in self.required_objects() {
            match (obj.validator)(ctx) {
                Ok(()) => report.present.push(obj.name),
                Err(issue) => report.missing.push((obj.name, issue)),
            }
        }
        report
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::degradation::Guidance;
    use std::cell::Cell;
    use std::rc::Rc;

    #[test]
    fn runtime_validation_reports_guidance() {
        struct RuntimeValidator;

        impl AttachedValidator for RuntimeValidator {
            fn required_objects(&self) -> Vec<RequiredObject> {
                vec![
                    RequiredObject {
                        name: "symbols table".to_string(),
                        store: StoreKind::Postgres,
                        validator: Box::new(|_| Ok(())),
                    },
                    RequiredObject {
                        name: "BM25 index".to_string(),
                        store: StoreKind::Postgres,
                        validator: Box::new(|_| {
                            Err(SetupIssue {
                                object_name: "BM25 index".to_string(),
                                store: "postgres".to_string(),
                                guidance: Guidance {
                                    problem: "BM25 index is missing".to_string(),
                                    action: "apply hub schema migrations".to_string(),
                                    command_hint: Some("gdaemon apply".to_string()),
                                },
                            })
                        }),
                    },
                ]
            }
        }

        let falkor_config = crate::config::FalkorConfig {
            host: "localhost".to_string(),
            port: 16379,
            password: None,
        };
        let mut ctx = ValidationContext {
            pg: None,
            falkor_config: Some(&falkor_config),
            qdrant_config: None,
        };

        let report = RuntimeValidator.validate(&mut ctx);

        assert!(!report.is_healthy());
        assert_eq!(report.present, vec!["symbols table"]);
        assert_eq!(report.missing.len(), 1);
        let (object, issue) = &report.missing[0];
        assert_eq!(object, "BM25 index");
        assert_eq!(issue.object_name, "BM25 index");
        assert_eq!(issue.guidance.problem, "BM25 index is missing");
        assert_eq!(
            issue.guidance.command_hint.as_deref(),
            Some("gdaemon apply")
        );
    }

    #[test]
    fn validator_can_query_through_mutable_context() {
        let falkor_config = crate::config::FalkorConfig {
            host: "graph.local".to_string(),
            port: 16379,
            password: None,
        };
        let mut ctx = ValidationContext {
            pg: None,
            falkor_config: Some(&falkor_config),
            qdrant_config: None,
        };
        let observed_port = Rc::new(Cell::new(None));
        let captured_port = Rc::clone(&observed_port);
        let mut validator = RequiredObject {
            name: "graph config".to_string(),
            store: StoreKind::FalkorDB,
            validator: Box::new(move |ctx| {
                captured_port.set(ctx.falkor_config.map(|config| config.port));
                Ok(())
            }),
        };

        (validator.validator)(&mut ctx).expect("validator can read mutable context");

        assert_eq!(observed_port.get(), Some(16379));
    }
}
