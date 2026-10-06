use super::{Pool, PoolError};
use tokio_postgres::{Error, Row, SimpleQueryMessage, types::ToSql};

/// One verified pool checkout for session-level work.
///
/// It consumes a slot from `max_size` while held. Dropping it discards the
/// connection, so locks and other session state cannot reach another borrower.
pub struct DedicatedSession {
    client: Option<deadpool_postgres::Client>,
}

impl Pool {
    /// Reserve a runtime-role session that will never be recycled.
    pub async fn dedicated_session(&self) -> Result<DedicatedSession, PoolError> {
        Ok(DedicatedSession {
            client: Some(self.get().await?),
        })
    }
}

impl DedicatedSession {
    /// Run a PostgreSQL simple query on this session.
    pub async fn simple_query(&self, query: &str) -> Result<Vec<SimpleQueryMessage>, Error> {
        self.client().simple_query(query).await
    }

    /// Run a query with positional parameters on this session.
    pub async fn query(
        &self,
        query: &str,
        params: &[&(dyn ToSql + Sync)],
    ) -> Result<Vec<Row>, Error> {
        self.client().query(query, params).await
    }

    /// Try the session-level advisory lock using the shared Python key format.
    pub async fn try_session_lock(&self, key: &str) -> Result<bool, Error> {
        self.client()
            .query_one("SELECT pg_try_advisory_lock(hashtext($1))", &[&key])
            .await?
            .try_get(0)
    }

    /// Release one acquisition of the session-level advisory lock.
    pub async fn session_unlock(&self, key: &str) -> Result<bool, Error> {
        self.client()
            .query_one("SELECT pg_advisory_unlock(hashtext($1))", &[&key])
            .await?
            .try_get(0)
    }

    fn client(&self) -> &deadpool_postgres::Client {
        // Only Drop takes the checkout, after all borrows of self have ended.
        self.client
            .as_ref()
            .expect("dedicated session owns its checkout")
    }
}

impl Drop for DedicatedSession {
    fn drop(&mut self) {
        if let Some(client) = self.client.take() {
            drop(deadpool_postgres::Object::take(client));
        }
    }
}
