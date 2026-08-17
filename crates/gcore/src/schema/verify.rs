use std::collections::{BTreeMap, BTreeSet};

use postgres::Client;
use serde::{Deserialize, Serialize};

use super::assets::{
    BASELINE_CHECKSUM, BASELINE_VERSION, CATALOG_MANIFEST_JSON, MIGRATIONS,
    PRIOR_RECEIPT_CHECKSUMS, SEED_MANIFEST_JSON,
};
use super::error::SchemaError;

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
    // Column ordinals differ when an additive migration reaches an existing database versus when
    // that same final definition is emitted by a flattened baseline. Runtime schema authority is
    // name-based, so catalog identity deliberately covers column semantics rather than position.
    // The independently managed gwiki_* projection can share this schema but is outside this
    // manifest's authority boundary.
    let columns = query_entries(
        client,
        r#"
        SELECT table_name || '.' || column_name AS name,
               concat_ws('|', data_type, udt_name, is_nullable,
                         COALESCE(column_default, ''), is_generated)
                   AS definition
        FROM information_schema.columns
        WHERE table_schema = $1
          AND substring(table_name from 1 for 6) <> 'gwiki_'
        ORDER BY table_name, column_name
        "#,
        schema,
        SchemaQualification::Omit,
    )?;
    // PostgreSQL 18 represents newly created NOT NULL declarations in pg_constraint while
    // upgraded databases can retain the equivalent column flag without those redundant rows.
    let constraints = query_entries(
        client,
        r#"
        SELECT relation.relname || '.' || constraint_record.conname AS name,
               pg_get_constraintdef(constraint_record.oid, true) AS definition
        FROM pg_constraint AS constraint_record
        JOIN pg_class AS relation ON relation.oid = constraint_record.conrelid
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = $1
          AND constraint_record.contype <> 'n'
          AND substring(relation.relname from 1 for 6) <> 'gwiki_'
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
          AND substring(tablename from 1 for 6) <> 'gwiki_'
        ORDER BY indexname
        "#,
        schema,
        SchemaQualification::Placeholder,
    )?;
    // Extension-owned routines follow the installed extension version and remain outside Gobby's
    // schema authority even when the extension places them in the application schema.
    let mut functions = query_entries(
        client,
        r#"
        SELECT routine.proname || '(' || pg_get_function_identity_arguments(routine.oid) || ')'
                   AS name,
               pg_get_functiondef(routine.oid) AS definition
        FROM pg_proc AS routine
        JOIN pg_namespace AS namespace ON namespace.oid = routine.pronamespace
        WHERE namespace.nspname = $1
          AND NOT EXISTS (
              SELECT 1
              FROM pg_depend AS dependency
              JOIN pg_extension AS extension ON extension.oid = dependency.refobjid
              WHERE dependency.classid = 'pg_proc'::regclass
                AND dependency.objid = routine.oid
                AND dependency.deptype = 'e'
          )
        ORDER BY name
        "#,
        schema,
        SchemaQualification::Placeholder,
    )?;
    for function in &mut functions {
        function.definition = strip_full_line_sql_comments(&function.definition);
    }
    let triggers = query_entries(
        client,
        r#"
        SELECT relation.relname || '.' || trigger_record.tgname AS name,
               pg_get_triggerdef(trigger_record.oid, true) AS definition
        FROM pg_trigger AS trigger_record
        JOIN pg_class AS relation ON relation.oid = trigger_record.tgrelid
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = $1 AND NOT trigger_record.tgisinternal
          AND substring(relation.relname from 1 for 6) <> 'gwiki_'
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

fn strip_full_line_sql_comments(value: &str) -> String {
    value
        .lines()
        .filter(|line| !line.trim_start().starts_with("--"))
        .collect::<Vec<_>>()
        .join("\n")
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
    let normalized = observed
        .iter()
        .map(|(version, (filename, checksum))| {
            let checksum = expected
                .get(version)
                .filter(|(expected_filename, expected_checksum)| {
                    filename == expected_filename
                        && (checksum == expected_checksum
                            || PRIOR_RECEIPT_CHECKSUMS.iter().any(
                                |(prior_version, prior_checksum)| {
                                    prior_version == version && prior_checksum == checksum
                                },
                            ))
                })
                .map(|(_, expected_checksum)| expected_checksum.clone())
                .unwrap_or_else(|| checksum.clone());
            (*version, (filename.clone(), checksum))
        })
        .collect::<BTreeMap<_, _>>();
    if normalized != expected {
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
                .any(|actual_row| contains_expected_seed_json(actual_row, &record.values, &table))
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

fn contains_expected_seed_json(
    actual: &serde_json::Value,
    expected: &serde_json::Value,
    table: &str,
) -> bool {
    match (actual, expected) {
        (serde_json::Value::Object(actual), serde_json::Value::Object(expected)) => expected
            .iter()
            .filter(|(field, _value)| !is_live_mutable_seed_field(table, field))
            .all(|(field, value)| {
                actual
                    .get(field)
                    .is_some_and(|actual| contains_expected_json(actual, value))
            }),
        _ => contains_expected_json(actual, expected),
    }
}

// Keep this projection aligned with scripts/schema_diff.py's live_mutable_columns contract. The
// baseline hash pins initial values; runtime verification protects only seed identity and fields
// that installed synchronization is not explicitly allowed to change.
fn is_live_mutable_seed_field(table: &str, field: &str) -> bool {
    match table {
        "projects" => matches!(
            field,
            "deleted_at"
                | "github_repo"
                | "github_url"
                | "linear_project_id"
                | "linear_sync_enabled"
                | "linear_synced_at"
                | "linear_team_id"
                | "repo_path"
        ),
        "sessions" => matches!(
            field,
            "approved_tools_json"
                | "chat_mode"
                | "context_injected"
                | "context_usage_confidence"
                | "context_usage_ratio"
                | "context_usage_source"
                | "context_usage_updated_at"
                | "context_used_tokens"
                | "context_window"
                | "digest_markdown"
                | "git_branch"
                | "had_edits"
                | "last_assistant_content"
                | "last_completion_output_tokens"
                | "last_digest_input_hash"
                | "last_digested_pair_index"
                | "last_prompt_cache_creation_tokens"
                | "last_prompt_cache_read_tokens"
                | "last_prompt_input_tokens"
                | "last_prompt_uncached_input_tokens"
                | "last_turn_markdown"
                | "message_count"
                | "model"
                | "original_prompt"
                | "parent_session_id"
                | "sandbox_enabled"
                | "sandbox_policy_hash"
                | "seq_num"
                | "status"
                | "summary_digest_turn_count"
                | "summary_generated_at"
                | "summary_generation_mode"
                | "summary_markdown"
                | "summary_path"
                | "summary_revision_id"
                | "summary_source_context_hash"
                | "terminal_context"
                | "title_source"
                | "tool_call_count"
                | "transcript_path"
                | "transcript_processed"
                | "turn_count"
                | "usage_cache_creation_tokens"
                | "usage_cache_read_tokens"
                | "usage_input_tokens"
                | "usage_output_tokens"
                | "workflow_name"
        ),
        "task_stages_registry" => matches!(
            field,
            "bundled_hash"
                | "category"
                | "default_agent"
                | "default_max_review_rounds"
                | "default_max_work_attempts"
                | "description"
                | "dispatch_inputs_json"
                | "dispatch_target"
                | "dispatch_type"
                | "display_label"
                | "is_terminal"
                | "position_hint"
                | "requires_human"
                | "review_policy"
                | "reviewer_agent"
                | "reviewer_agent_selector_json"
        ),
        "task_type_default_stages" => field == "position",
        _ => false,
    }
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
