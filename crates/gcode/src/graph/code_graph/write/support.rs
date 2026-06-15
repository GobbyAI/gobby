use anyhow::Context as _;

use crate::graph::typed_query::{TypedQuery, TypedValue};
use gobby_core::falkor::GraphClient;

pub(super) fn execute_write_query(
    client: &mut GraphClient,
    query: TypedQuery,
) -> anyhow::Result<()> {
    let TypedQuery { cypher, params } = query;
    client.query(&cypher, Some(params))?;
    Ok(())
}

pub(super) fn typed_query<I, K>(cypher: impl Into<String>, params: I) -> anyhow::Result<TypedQuery>
where
    I: IntoIterator<Item = (K, TypedValue)>,
    K: Into<String>,
{
    Ok(TypedQuery::with_params(cypher, params)?)
}

pub(super) fn usize_value(value: usize) -> anyhow::Result<TypedValue> {
    Ok(TypedValue::Integer(i64::try_from(value).context(
        "graph integer value exceeds FalkorDB i64 range",
    )?))
}

pub(super) fn sync_token_param(sync_token: &str) -> (&'static str, TypedValue) {
    ("sync_token", TypedValue::String(sync_token.to_string()))
}
