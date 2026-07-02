//! Conversions between gcode's domain-string ids and native-uuid SQL values.
//!
//! The hub stores every `code_*` id column as PostgreSQL `uuid`. Domain structs
//! keep ids as `String`/`Option<String>` (all UUIDv5-generated), so conversion
//! happens only at the SQL boundary: parse on bind, `to_string` on read.

use std::fmt::Display;

use anyhow::Context as _;
use postgres::Row;
use postgres::row::RowIndex;
use uuid::Uuid;

/// Parse a domain id string for binding against a native-uuid column.
pub fn id_param(value: &str) -> anyhow::Result<Uuid> {
    Uuid::parse_str(value).with_context(|| format!("invalid uuid id `{value}`"))
}

/// Parse an optional-id domain string; the `""` sentinel becomes SQL `NULL`.
pub fn opt_id_param(value: &str) -> anyhow::Result<Option<Uuid>> {
    if value.is_empty() {
        Ok(None)
    } else {
        Ok(Some(id_param(value)?))
    }
}

/// Parse a batch of domain id strings for uuid-array binds.
pub fn id_params(values: &[String]) -> anyhow::Result<Vec<Uuid>> {
    values.iter().map(|value| id_param(value)).collect()
}

/// Read a uuid column into the domain `String` representation.
pub fn id_string<I>(row: &Row, idx: I) -> anyhow::Result<String>
where
    I: RowIndex + Display,
{
    Ok(row.try_get::<_, Uuid>(idx)?.to_string())
}

/// Read a nullable uuid column into an `Option<String>`.
pub fn opt_id_string<I>(row: &Row, idx: I) -> anyhow::Result<Option<String>>
where
    I: RowIndex + Display,
{
    Ok(row
        .try_get::<_, Option<Uuid>>(idx)?
        .map(|id| id.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn id_param_parses_canonical_uuid_strings() {
        let id = "403e2117-92e7-5390-ad83-226629486481";
        assert_eq!(id_param(id).expect("parse uuid").to_string(), id);
    }

    #[test]
    fn id_param_rejects_non_uuid_ids_with_context() {
        let error = id_param("not-a-uuid").expect_err("non-uuid must fail");
        assert!(error.to_string().contains("not-a-uuid"));
    }

    #[test]
    fn opt_id_param_maps_empty_sentinel_to_none() {
        assert_eq!(opt_id_param("").expect("empty sentinel"), None);
        assert!(
            opt_id_param("403e2117-92e7-5390-ad83-226629486481")
                .expect("parse uuid")
                .is_some()
        );
    }

    #[test]
    fn id_params_parses_batches() {
        let ids = vec![
            "403e2117-92e7-5390-ad83-226629486481".to_string(),
            "d28e80d3-a95e-5c2a-91c3-92551f75a2b1".to_string(),
        ];
        let parsed = id_params(&ids).expect("parse batch");
        assert_eq!(parsed.len(), 2);
        assert!(id_params(&["bogus".to_string()]).is_err());
    }
}
