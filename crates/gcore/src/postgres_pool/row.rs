//! Row mapping for family repositories.
//!
//! A family crate's repository is a set of free functions that take a
//! `&Transaction` and return `FromRow` types, for example
//! `async fn get_task(tx: &Transaction<'_>, id: &str) -> Result<Option<Task>, TransactionError>`
//! over `tx.query_opt_as::<Task>(...)`. Domain values convert through their
//! `FromSql` impls, so every mapping failure is a column read error.

use tokio_postgres::Row;
use tokio_postgres::types::ToSql;

use super::transaction::{Transaction, TransactionError};

/// A type built from one result row.
pub trait FromRow: Sized {
    fn from_row(row: &Row) -> Result<Self, RowError>;
}

#[derive(Debug, thiserror::Error)]
#[error("a PostgreSQL row does not map to its type")]
pub struct RowError(#[from] tokio_postgres::Error);

impl Transaction<'_> {
    pub async fn query_as<T: FromRow>(
        &self,
        statement: &str,
        params: &[&(dyn ToSql + Sync)],
    ) -> Result<Vec<T>, TransactionError> {
        let rows = self.query(statement, params).await?;
        Ok(rows
            .iter()
            .map(T::from_row)
            .collect::<Result<Vec<_>, RowError>>()?)
    }

    pub async fn query_opt_as<T: FromRow>(
        &self,
        statement: &str,
        params: &[&(dyn ToSql + Sync)],
    ) -> Result<Option<T>, TransactionError> {
        let row = self.query_opt(statement, params).await?;
        Ok(row.as_ref().map(T::from_row).transpose()?)
    }
}
