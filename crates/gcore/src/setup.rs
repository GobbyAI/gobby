//! Shared setup-mode boundary.
//!
//! Attached and standalone setup contracts belong here. Runtime callers should
//! validate externally managed state explicitly and avoid implicit schema or
//! service creation.

pub use crate::degradation::{Guidance, SetupIssue};

/// Datastore kind for setup object classification.
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
/// Contains optional mutable connections to each datastore. Consumers use
/// whichever connection their validator needs; `None` means the service is not
/// configured. PostgreSQL is feature-gated because `postgres::Client::query`
/// requires `&mut self`.
pub struct ValidationContext<'a> {
    /// PostgreSQL connection supplied by the caller when the `postgres` feature is enabled.
    #[cfg(feature = "postgres")]
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

/// Required object that a consumer crate declares for setup validation.
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

/// Context supplied to standalone setup creation callbacks.
///
/// PostgreSQL is feature-gated because `postgres::Client::execute` requires
/// `&mut self` for DDL and DML operations.
pub struct SetupContext<'a> {
    /// PostgreSQL connection supplied by the caller when the `postgres` feature is enabled.
    #[cfg(feature = "postgres")]
    pub pg: Option<&'a mut postgres::Client>,
    /// FalkorDB connection configuration, when configured.
    pub falkor_config: Option<&'a crate::config::FalkorConfig>,
    /// Qdrant connection configuration, when configured.
    pub qdrant_config: Option<&'a crate::config::QdrantConfig>,
    /// If true, skip prompts and apply defaults.
    pub non_interactive: bool,
}

/// Report from a standalone setup creation run.
#[derive(Debug, Default)]
pub struct SetupReport {
    /// Objects successfully created.
    pub created: Vec<String>,
    /// Objects that already existed and were skipped.
    pub skipped: Vec<String>,
    /// Objects that failed creation, with error detail.
    pub failed: Vec<(String, String)>,
}

/// Error from standalone setup creation.
#[derive(Debug, thiserror::Error)]
pub enum SetupError {
    /// Connection setup failed for a datastore.
    #[error("connection failed for {store}: {message}")]
    ConnectionFailed {
        /// Store name.
        store: String,
        /// Diagnostic message.
        message: String,
    },
    /// Object creation failed.
    #[error("creation failed for {object}: {message}")]
    CreationFailed {
        /// Object name.
        object: String,
        /// Diagnostic message.
        message: String,
    },
    /// Creation was attempted in attached mode.
    #[error("setup refused in attached mode — use standalone setup")]
    AttachedModeRefused,
}

/// Consumer-supplied creation callback for an owned object.
pub type OwnedCreator = dyn for<'ctx> FnMut(&mut SetupContext<'ctx>) -> Result<(), SetupError>;

/// An object that a consumer crate owns and can create in standalone mode.
pub struct OwnedObject {
    /// Human-readable name, such as `gcode_symbols table`.
    pub name: String,
    /// Store kind that owns the object.
    pub store: StoreKind,
    /// Consumer-supplied creation function.
    pub creator: Box<OwnedCreator>,
}

/// Standalone-mode setup: explicit opt-in creation of consumer-owned resources.
pub trait StandaloneSetup {
    /// Namespace prefix for this consumer's owned resources, such as `gcode` or `gwiki`.
    fn namespace(&self) -> &str;

    /// Declare what this consumer owns and can create.
    fn owned_objects(&self) -> Result<Vec<OwnedObject>, SetupError>;

    /// Create consumer-owned resources. Called only on an explicit setup command.
    fn create(&self, ctx: &mut SetupContext<'_>) -> Result<SetupReport, SetupError>;
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::{Cell, RefCell};
    use std::rc::Rc;

    #[test]
    fn runtime_validation_reports_setup_guidance() {
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
                                    action: "run the standalone setup command".to_string(),
                                    command_hint: Some("gobby setup standalone".to_string()),
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
            #[cfg(feature = "postgres")]
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
            Some("gobby setup standalone")
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
            #[cfg(feature = "postgres")]
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

    #[test]
    fn creator_executes_without_moving_ownership() {
        let mut ctx = SetupContext {
            #[cfg(feature = "postgres")]
            pg: None,
            falkor_config: None,
            qdrant_config: None,
            non_interactive: true,
        };
        let calls = Rc::new(RefCell::new(Vec::new()));
        let first_calls = Rc::clone(&calls);
        let second_calls = Rc::clone(&calls);
        let mut creators = vec![
            OwnedObject {
                name: "first table".to_string(),
                store: StoreKind::Postgres,
                creator: Box::new(move |ctx| {
                    assert!(ctx.non_interactive);
                    first_calls.borrow_mut().push("first");
                    Ok(())
                }),
            },
            OwnedObject {
                name: "second table".to_string(),
                store: StoreKind::Postgres,
                creator: Box::new(move |ctx| {
                    assert!(ctx.non_interactive);
                    second_calls.borrow_mut().push("second");
                    Ok(())
                }),
            },
        ];

        for creator in &mut creators {
            (creator.creator)(&mut ctx).expect("creator can execute through mutable context");
        }

        assert!(ctx.non_interactive);
        assert_eq!(*calls.borrow(), vec!["first", "second"]);
    }
}
