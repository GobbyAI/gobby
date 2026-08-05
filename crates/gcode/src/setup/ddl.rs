use super::contracts::NAMESPACE;
use gobby_core::schema::gcode_postgres_objects;
use gobby_core::setup::{
    OwnedObject, SetupContext, SetupError, SetupReport, StandaloneSetup, StoreKind,
};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GcodeStandaloneSetup {
    schema: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct PostgresObjectDefinition {
    pub(crate) name: String,
    pub(crate) sql: String,
}

impl GcodeStandaloneSetup {
    pub fn new(schema: impl Into<String>) -> Self {
        Self {
            schema: schema.into(),
        }
    }

    pub fn schema(&self) -> &str {
        &self.schema
    }

    pub(crate) fn postgres_object_definitions(
        &self,
    ) -> Result<Vec<PostgresObjectDefinition>, SetupError> {
        gcode_postgres_objects(&self.schema)
            .map(|definitions| {
                definitions
                    .into_iter()
                    .map(|definition| PostgresObjectDefinition {
                        name: definition.name.to_string(),
                        sql: definition.sql,
                    })
                    .collect()
            })
            .map_err(|error| SetupError::CreationFailed {
                object: error.object,
                message: error.message,
            })
    }
}

impl StandaloneSetup for GcodeStandaloneSetup {
    fn namespace(&self) -> &str {
        NAMESPACE
    }

    fn owned_objects(&self) -> Result<Vec<OwnedObject>, SetupError> {
        Ok(self
            .postgres_object_definitions()?
            .into_iter()
            .map(owned_object)
            .collect())
    }

    fn create(&self, ctx: &mut SetupContext<'_>) -> Result<SetupReport, SetupError> {
        let mut report = SetupReport::default();
        let mut objects = self.owned_objects()?.into_iter();
        while let Some(mut object) = objects.next() {
            match (object.creator)(ctx) {
                Ok(()) => report.created.push(object.name),
                Err(err) => {
                    report.failed.push((object.name, err.to_string()));
                    report.skipped.extend(objects.map(|object| object.name));
                    break;
                }
            }
        }
        Ok(report)
    }
}

fn owned_object(definition: PostgresObjectDefinition) -> OwnedObject {
    let object_name = definition.name;
    let sql = definition.sql;
    OwnedObject {
        name: object_name.clone(),
        store: StoreKind::Postgres,
        creator: Box::new(move |ctx| execute_postgres_ddl(ctx, &object_name, &sql)),
    }
}

fn execute_postgres_ddl(
    ctx: &mut SetupContext<'_>,
    object: &str,
    sql: &str,
) -> Result<(), SetupError> {
    let Some(pg) = ctx.pg.as_deref_mut() else {
        return Err(SetupError::ConnectionFailed {
            store: "postgres".to_string(),
            message: "PostgreSQL connection was not supplied to setup context".to_string(),
        });
    };

    pg.batch_execute(sql)
        .map_err(|err| SetupError::CreationFailed {
            object: object.to_string(),
            message: err.to_string(),
        })
}
