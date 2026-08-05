use std::collections::{BTreeMap, BTreeSet};

use postgres::Client;
use serde::{Deserialize, Serialize};

use super::assets::{
    BASELINE_CHECKSUM, BASELINE_VERSION, CATALOG_MANIFEST_JSON, MIGRATIONS, SEED_MANIFEST_JSON,
};
use super::runner::SchemaError;

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CatalogEntry {
    pub name: String,
    pub definition: String,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CatalogManifest {
    pub columns: Vec<CatalogEntry>,
    pub constraints: Vec<CatalogEntry>,
    pub functions: Vec<CatalogEntry>,
    pub indexes: Vec<CatalogEntry>,
    pub triggers: Vec<CatalogEntry>,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct VerificationReport {
    pub checked_receipts: usize,
    pub checked_seed_rows: usize,
    pub checked_catalog_objects: usize,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct SeedRecord {
    #[serde(rename = "key")]
    _key: Vec<serde_json::Value>,
    values: serde_json::Value,
}

pub fn catalog_manifest(client: &mut Client, schema: &str) -> Result<CatalogManifest, SchemaError> {
    validate_identifier(schema)?;
    let columns = query_entries(
        client,
        r#"
        SELECT table_name || '.' || column_name AS name,
               concat_ws('|', ordinal_position::text, data_type, udt_name,
                         is_nullable, COALESCE(column_default, ''), is_generated)
                   AS definition
        FROM information_schema.columns
        WHERE table_schema = $1
        ORDER BY table_name, ordinal_position
        "#,
        schema,
        SchemaQualification::Omit,
    )?;
    let constraints = query_entries(
        client,
        r#"
        SELECT relation.relname || '.' || constraint_record.conname AS name,
               pg_get_constraintdef(constraint_record.oid, true) AS definition
        FROM pg_constraint AS constraint_record
        JOIN pg_class AS relation ON relation.oid = constraint_record.conrelid
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = $1
        ORDER BY relation.relname, constraint_record.conname
        "#,
        schema,
        SchemaQualification::Omit,
    )?;
    let indexes = query_entries(
        client,
        r#"
        SELECT indexname AS name, indexdef AS definition
        FROM pg_indexes
        WHERE schemaname = $1
        ORDER BY indexname
        "#,
        schema,
        SchemaQualification::Placeholder,
    )?;
    let functions = query_entries(
        client,
        r#"
        SELECT routine.proname || '(' || pg_get_function_identity_arguments(routine.oid) || ')'
                   AS name,
               pg_get_functiondef(routine.oid) AS definition
        FROM pg_proc AS routine
        JOIN pg_namespace AS namespace ON namespace.oid = routine.pronamespace
        WHERE namespace.nspname = $1
        ORDER BY name
        "#,
        schema,
        SchemaQualification::Placeholder,
    )?;
    let triggers = query_entries(
        client,
        r#"
        SELECT relation.relname || '.' || trigger_record.tgname AS name,
               pg_get_triggerdef(trigger_record.oid, true) AS definition
        FROM pg_trigger AS trigger_record
        JOIN pg_class AS relation ON relation.oid = trigger_record.tgrelid
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = $1 AND NOT trigger_record.tgisinternal
        ORDER BY relation.relname, trigger_record.tgname
        "#,
        schema,
        SchemaQualification::Omit,
    )?;
    Ok(CatalogManifest {
        columns,
        constraints,
        functions,
        indexes,
        triggers,
    })
}

pub fn render_catalog_manifest(manifest: &CatalogManifest) -> Result<String, SchemaError> {
    Ok(serde_json::to_string_pretty(manifest)? + "\n")
}

pub(crate) fn verify_schema(
    client: &mut Client,
    schema: &str,
) -> Result<VerificationReport, SchemaError> {
    validate_identifier(schema)?;
    let checked_receipts = verify_receipts(client, schema)?;
    let expected: CatalogManifest = serde_json::from_str(CATALOG_MANIFEST_JSON)?;
    let observed = catalog_manifest(client, schema)?;
    if observed != expected {
        return Err(SchemaError::Verification(catalog_diff(
            &expected, &observed,
        )));
    }
    let checked_seed_rows = verify_seed_rows(client, schema)?;
    Ok(VerificationReport {
        checked_receipts,
        checked_seed_rows,
        checked_catalog_objects: observed.columns.len()
            + observed.constraints.len()
            + observed.functions.len()
            + observed.indexes.len()
            + observed.triggers.len(),
    })
}

#[derive(Clone, Copy)]
enum SchemaQualification {
    Omit,
    Placeholder,
}

fn query_entries(
    client: &mut Client,
    query: &str,
    schema: &str,
    qualification: SchemaQualification,
) -> Result<Vec<CatalogEntry>, SchemaError> {
    let mut entries = client
        .query(query, &[&schema])?
        .into_iter()
        .map(|row| CatalogEntry {
            name: normalize_schema(row.get::<_, String>(0), schema, qualification),
            definition: normalize_schema(row.get::<_, String>(1), schema, qualification),
        })
        .collect::<Vec<_>>();
    entries.sort();
    Ok(entries)
}

fn normalize_schema(mut value: String, schema: &str, qualification: SchemaQualification) -> String {
    let replacement = match qualification {
        SchemaQualification::Omit => "",
        SchemaQualification::Placeholder => "$schema.",
    };
    value = value.replace(&format!("\"{schema}\"."), replacement);
    value = value.replace(&format!("{schema}."), replacement);
    value = value.replace(&format!("IN SCHEMA \"{schema}\""), "IN SCHEMA $schema");
    value.replace(&format!("IN SCHEMA {schema}"), "IN SCHEMA $schema")
}

fn verify_receipts(client: &mut Client, schema: &str) -> Result<usize, SchemaError> {
    let qualified = qualified_name(schema, "schema_migrations")?;
    let rows = client.query(
        &format!("SELECT version, filename, checksum FROM {qualified} ORDER BY version"),
        &[],
    )?;
    let mut expected = BTreeMap::from([(
        BASELINE_VERSION,
        (
            format!("baseline@{BASELINE_VERSION}"),
            BASELINE_CHECKSUM.to_owned(),
        ),
    )]);
    for migration in MIGRATIONS {
        expected.insert(
            migration.version,
            (migration.filename.to_owned(), migration.checksum.to_owned()),
        );
    }
    let observed = rows
        .into_iter()
        .map(|row| {
            (
                row.get::<_, i32>(0),
                (
                    row.get::<_, Option<String>>(1).unwrap_or_default(),
                    row.get::<_, Option<String>>(2).unwrap_or_default(),
                ),
            )
        })
        .collect::<BTreeMap<_, _>>();
    if observed != expected {
        return Err(SchemaError::Verification(format!(
            "schema receipt drift: expected {expected:?}, observed {observed:?}"
        )));
    }
    Ok(observed.len())
}

fn verify_seed_rows(client: &mut Client, schema: &str) -> Result<usize, SchemaError> {
    let expected: BTreeMap<String, Vec<SeedRecord>> = serde_json::from_str(SEED_MANIFEST_JSON)?;
    let mut checked = 0;
    for (table, records) in expected {
        validate_identifier(&table)?;
        let qualified = qualified_name(schema, &table)?;
        let actual = client
            .query(
                &format!("SELECT to_jsonb(seed_row)::text FROM {qualified} AS seed_row"),
                &[],
            )?
            .into_iter()
            .map(|row| serde_json::from_str::<serde_json::Value>(&row.get::<_, String>(0)))
            .collect::<Result<Vec<_>, _>>()?;
        for record in records {
            checked += 1;
            if !actual
                .iter()
                .any(|actual_row| contains_expected_json(actual_row, &record.values))
            {
                return Err(SchemaError::Verification(format!(
                    "canonical seed drift in {table}: missing {}",
                    record.values
                )));
            }
        }
    }
    Ok(checked)
}

fn contains_expected_json(actual: &serde_json::Value, expected: &serde_json::Value) -> bool {
    match (actual, expected) {
        (serde_json::Value::Object(actual), serde_json::Value::Object(expected)) => {
            expected.iter().all(|(key, value)| {
                actual
                    .get(key)
                    .is_some_and(|actual| contains_expected_json(actual, value))
            })
        }
        (serde_json::Value::Array(actual), serde_json::Value::Array(expected)) => {
            actual == expected
        }
        _ => actual == expected,
    }
}

fn catalog_diff(expected: &CatalogManifest, observed: &CatalogManifest) -> String {
    let expected = catalog_entry_set(expected);
    let observed = catalog_entry_set(observed);
    let missing = expected
        .difference(&observed)
        .take(5)
        .cloned()
        .collect::<Vec<_>>();
    let unexpected = observed
        .difference(&expected)
        .take(5)
        .cloned()
        .collect::<Vec<_>>();
    format!("catalog manifest drift: missing {missing:?}; unexpected {unexpected:?}")
}

fn catalog_entry_set(manifest: &CatalogManifest) -> BTreeSet<String> {
    let mut entries = BTreeSet::new();
    for (kind, values) in [
        ("column", &manifest.columns),
        ("constraint", &manifest.constraints),
        ("function", &manifest.functions),
        ("index", &manifest.indexes),
        ("trigger", &manifest.triggers),
    ] {
        entries.extend(
            values
                .iter()
                .map(|entry| format!("{kind}:{}={}", entry.name, entry.definition)),
        );
    }
    entries
}

pub(crate) fn qualified_name(schema: &str, relation: &str) -> Result<String, SchemaError> {
    validate_identifier(schema)?;
    validate_identifier(relation)?;
    Ok(format!("\"{schema}\".\"{relation}\""))
}

pub(crate) fn validate_identifier(value: &str) -> Result<(), SchemaError> {
    let mut bytes = value.bytes();
    let valid = matches!(bytes.next(), Some(byte) if byte.is_ascii_alphabetic() || byte == b'_')
        && bytes.all(|byte| byte.is_ascii_alphanumeric() || byte == b'_');
    if valid {
        Ok(())
    } else {
        Err(SchemaError::InvalidSchema(value.to_owned()))
    }
}
