use std::collections::{BTreeMap, BTreeSet};

use postgres::Client;
use serde::{Deserialize, Serialize};

use super::assets::{
    BASELINE_CHECKSUM, BASELINE_VERSION, CATALOG_MANIFEST_JSON, MIGRATIONS,
    PRIOR_RECEIPT_CHECKSUMS, SEED_MANIFEST_JSON, baseline_filename, is_prior_baseline_receipt,
};
use super::error::SchemaError;
use super::runner::auth_schema_for;

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
    // Retired projection objects are included so verification detects accidental recreation.
    let columns = query_entries(
        client,
        r#"
        SELECT table_name || '.' || column_name AS name,
               concat_ws('|', data_type, udt_name, is_nullable,
                         COALESCE(column_default, ''), is_generated)
                   AS definition
        FROM information_schema.columns
        WHERE table_schema = $1
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
    // Extension-owned routines follow the installed extension version and remain outside Gobby's
    // schema authority even when the extension places them in the application schema.
    let auth_schema = auth_schema_for(schema);
    let extension_functions = client
        .query(
            "SELECT namespace.nspname, routine.proname, extension.extname
         FROM pg_proc AS routine
         JOIN pg_namespace AS namespace ON namespace.oid = routine.pronamespace
         JOIN pg_depend AS dependency ON dependency.objid = routine.oid
         JOIN pg_extension AS extension ON extension.oid = dependency.refobjid
         WHERE dependency.classid = 'pg_proc'::regclass AND dependency.deptype = 'e'",
            &[],
        )?
        .into_iter()
        .map(|row| {
            (
                (row.get::<_, String>(0), row.get::<_, String>(1)),
                format!("$extension_{}", row.get::<_, String>(2)),
            )
        })
        .collect::<BTreeMap<_, _>>();
    let mut functions = client
        .query(
            r#"
        SELECT format('%I.%I(%s)', namespace.nspname, routine.proname,
                      pg_get_function_identity_arguments(routine.oid))
                   AS name,
               pg_get_functiondef(routine.oid) AS definition
        FROM pg_proc AS routine
        JOIN pg_namespace AS namespace ON namespace.oid = routine.pronamespace
        WHERE namespace.nspname IN ($1, $2)
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
            &[&schema, &auth_schema.as_ref()],
        )?
        .into_iter()
        .map(|row| CatalogEntry {
            name: normalize_function_sql(&row.get::<_, String>(0), schema, &auth_schema, false),
            definition: normalize_function_definition(
                &row.get::<_, String>(1),
                schema,
                &auth_schema,
                &extension_functions,
            ),
        })
        .collect::<Vec<_>>();
    functions.sort();
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

// pg_get_functiondef wraps the source in a dollar quote. Only that outer wrapper is SQL
// source: nested dollar quotes and string literals inside it are data and must remain exact.
fn normalize_function_definition(
    value: &str,
    schema: &str,
    auth_schema: &str,
    extensions: &BTreeMap<(String, String), String>,
) -> String {
    let value = value.trim_end_matches('\n');
    // PostgreSQL chooses a delimiter absent from the source. Work backwards from its
    // closing delimiter, so an AS-like sequence in a quoted argument default cannot win.
    let body = value.strip_suffix('$').and_then(|prefix| {
        let delimiter = &value[prefix.rfind('$')?..];
        if dollar_delimiter(delimiter) != Some(delimiter) {
            return None;
        }
        let prefix = value.strip_suffix(delimiter)?;
        let (header, source) = prefix.rsplit_once(&format!("\nAS {delimiter}"))?;
        Some((header, delimiter, source))
    });
    let header = body.map_or(value, |(header, _, _)| header);
    let header =
        normalize_function_sql_with_extensions(header, schema, auth_schema, false, extensions);
    match body {
        Some((_, delimiter, source)) => format!(
            "{header}\nAS {delimiter}{}{delimiter}",
            normalize_function_sql_with_extensions(source, schema, auth_schema, true, extensions),
        ),
        None => header.trim_end_matches('\n').to_owned(),
    }
}

// Normalize schema identifier tokens, never substrings of identifiers or quoted data.
// Preserve formatting except established full-line SQL comments outside quoted text.
fn normalize_function_sql(
    value: &str,
    schema: &str,
    auth_schema: &str,
    strip_comments: bool,
) -> String {
    normalize_function_sql_with_extensions(
        value,
        schema,
        auth_schema,
        strip_comments,
        &BTreeMap::new(),
    )
}

fn normalize_function_sql_with_extensions(
    value: &str,
    schema: &str,
    auth_schema: &str,
    strip_comments: bool,
    extensions: &BTreeMap<(String, String), String>,
) -> String {
    let bytes = value.as_bytes();
    let mut output = String::with_capacity(value.len());
    let mut i = 0;
    let mut line_start = 0;
    while i < bytes.len() {
        let start = i;
        if bytes[i..].starts_with(b"--") {
            let end = value[i..]
                .find('\n')
                .map_or(bytes.len(), |offset| i + offset);
            if strip_comments && value[line_start..i].trim().is_empty() {
                output.truncate(output.len() - (i - line_start));
                i = (end + 1).min(bytes.len());
                line_start = i;
                continue;
            }
            i = end;
        } else if bytes[i..].starts_with(b"/*") {
            i += 2;
            let mut depth = 1;
            while i < bytes.len() && depth > 0 {
                if bytes[i..].starts_with(b"/*") {
                    depth += 1;
                    i += 2;
                } else if bytes[i..].starts_with(b"*/") {
                    depth -= 1;
                    i += 2;
                } else {
                    i += 1;
                }
            }
        } else if bytes[i] == b'\'' || bytes[i] == b'"' {
            let quote = bytes[i];
            let escaped = quote == b'\''
                && i > 0
                && matches!(bytes[i - 1], b'e' | b'E')
                && (i < 2 || !identifier_byte(bytes[i - 2]));
            i += 1;
            while i < bytes.len() {
                if escaped && bytes[i] == b'\\' {
                    i = (i + 2).min(bytes.len());
                } else if bytes[i] == quote {
                    i += 1;
                    if bytes.get(i) == Some(&quote) {
                        i += 1;
                    } else {
                        break;
                    }
                } else {
                    i += 1;
                }
            }
            if quote == b'"' && bytes.get(i) == Some(&b'.') {
                let identifier = &value[start + 1..i - 1];
                if let Some(replacement) =
                    extension_qualification(identifier, &value[i + 1..], extensions)
                {
                    output.push_str(replacement);
                    continue;
                }
                if let Some(replacement) = schema_placeholder(identifier, schema, auth_schema) {
                    output.push_str(replacement);
                    continue;
                }
            }
            if quote == b'\''
                && value[line_start..start]
                    .trim_start()
                    .starts_with("SET search_path TO ")
                && &value[start + 1..i - 1] == auth_schema
            {
                output.push('\'');
                output.push_str("$auth_schema");
                output.push('\'');
                continue;
            }
        } else if bytes[i] == b'$' && dollar_delimiter(&value[i..]).is_some() {
            let delimiter = dollar_delimiter(&value[i..]).expect("checked delimiter");
            i += delimiter.len();
            i = value[i..]
                .find(delimiter)
                .map_or(bytes.len(), |offset| i + offset + delimiter.len());
        } else if bytes[i].is_ascii_alphabetic() || bytes[i] == b'_' || !bytes[i].is_ascii() {
            i += 1;
            while i < bytes.len() && identifier_byte(bytes[i]) {
                i += 1;
            }
            if bytes.get(i) == Some(&b'.') {
                if let Some(replacement) =
                    extension_qualification(&value[start..i], &value[i + 1..], extensions)
                {
                    output.push_str(replacement);
                    continue;
                }
                if let Some(replacement) = schema_placeholder(&value[start..i], schema, auth_schema)
                {
                    output.push_str(replacement);
                    continue;
                }
            }
        } else {
            i += value[i..]
                .chars()
                .next()
                .expect("nonempty remainder")
                .len_utf8();
        }
        output.push_str(&value[start..i]);
        if let Some(offset) = value[start..i].rfind('\n') {
            line_start = start + offset + 1;
        }
    }
    output
}

fn extension_qualification<'a>(
    schema: &str,
    rest: &str,
    extensions: &'a BTreeMap<(String, String), String>,
) -> Option<&'a str> {
    let function = rest.split('(').next()?.trim_end();
    let function = function
        .strip_prefix('"')
        .and_then(|name| name.strip_suffix('"'))
        .unwrap_or(function);
    extensions
        .get(&(schema.to_owned(), function.to_owned()))
        .map(String::as_str)
}

fn identifier_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'$') || !byte.is_ascii()
}

fn schema_placeholder(identifier: &str, schema: &str, auth_schema: &str) -> Option<&'static str> {
    if identifier == schema {
        Some("$schema")
    } else if identifier == auth_schema {
        Some("$auth_schema")
    } else {
        None
    }
}

#[cfg(test)]
#[path = "verify_tests.rs"]
mod tests;

fn dollar_delimiter(value: &str) -> Option<&str> {
    let end = value[1..].find('$')? + 1;
    let tag = &value[1..end];
    if tag.is_empty()
        || (tag.as_bytes()[0].is_ascii_alphabetic()
            || tag.starts_with('_')
            || !tag.as_bytes()[0].is_ascii())
            && tag
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || !byte.is_ascii())
    {
        Some(&value[..=end])
    } else {
        None
    }
}

fn verify_receipts(client: &mut Client, schema: &str) -> Result<usize, SchemaError> {
    let qualified = qualified_name(schema, "schema_migrations")?;
    let rows = client.query(
        &format!("SELECT version, filename, checksum FROM {qualified} ORDER BY version"),
        &[],
    )?;
    let mut expected = BTreeMap::from([(
        BASELINE_VERSION,
        (baseline_filename(), BASELINE_CHECKSUM.to_owned()),
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
            if is_prior_baseline_receipt(*version, filename, checksum) {
                return (
                    BASELINE_VERSION,
                    (baseline_filename(), BASELINE_CHECKSUM.to_owned()),
                );
            }
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
                | "heuristic_title"
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
                | "reasoning_effort"
                | "sandbox_enabled"
                | "sandbox_policy_hash"
                | "seq_num"
                | "status"
                | "summary_digest_turn_count"
                | "summary_generated_at"
                | "summary_generation_mode"
                | "summary_markdown"
                | "summary_path"
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
