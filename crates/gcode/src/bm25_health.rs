use postgres::Client;
use serde::Serialize;

use crate::postgres_errors;

pub const REPAIR_COMMAND: &str = "gobby postgres repair-code-index";
pub const REQUIRED_INDEXES: [&str; 2] = [
    "public.code_symbols_search_bm25",
    "public.code_content_search_bm25",
];

const CORRUPTION_SQLSTATES: [&str; 3] = ["XX000", "XX001", "XX002"];

#[derive(Debug, Serialize)]
pub struct Bm25Health {
    pub healthy: bool,
    pub repair_command: &'static str,
    pub indexes: Vec<Bm25IndexHealth>,
}

#[derive(Debug, Serialize)]
pub struct Bm25IndexHealth {
    pub name: &'static str,
    pub state: &'static str,
    pub repaired: bool,
    pub checks: Vec<Bm25IndexCheck>,
    pub error: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct Bm25IndexCheck {
    pub name: String,
    pub passed: bool,
    pub details: Option<String>,
}

pub fn verify(conn: &mut Client) -> Bm25Health {
    let indexes = REQUIRED_INDEXES
        .into_iter()
        .map(|name| verify_index(conn, name))
        .collect::<Vec<_>>();
    Bm25Health {
        healthy: indexes.iter().all(|index| index.state == "healthy"),
        repair_command: REPAIR_COMMAND,
        indexes,
    }
}

impl Bm25Health {
    pub fn render_text(&self) -> String {
        let mut lines = vec![format!(
            "  BM25:     {}",
            if self.healthy { "healthy" } else { "degraded" }
        )];
        for index in &self.indexes {
            lines.push(format!("    {}: {}", index.name, index.state));
            if let Some(error) = &index.error {
                lines.push(format!("      Error: {error}"));
            }
        }
        if !self.healthy {
            lines.push(format!("    Repair: {}", self.repair_command));
        }
        lines.join("\n")
    }
}

fn verify_index(conn: &mut Client, name: &'static str) -> Bm25IndexHealth {
    let exists = conn
        .query_one("SELECT to_regclass($1::TEXT)::TEXT", &[&name])
        .and_then(|row| row.try_get::<_, Option<String>>(0));
    match exists {
        Ok(None) => {
            return index_health(
                name,
                "missing",
                Some("required BM25 index is missing; run PostgreSQL setup/migrations".into()),
            );
        }
        Err(error) => return verification_error(name, &error),
        Ok(Some(_)) => {}
    }

    match conn.query(
        "SELECT check_name, passed, details
         FROM pdb.verify_index($1::TEXT::regclass, on_error_stop => true)",
        &[&name],
    ) {
        Ok(rows) => {
            let checks = rows
                .into_iter()
                .map(|row| Bm25IndexCheck {
                    name: row.get("check_name"),
                    passed: row.get("passed"),
                    details: row.get("details"),
                })
                .collect::<Vec<_>>();
            if checks.is_empty() {
                return index_health(
                    name,
                    "error",
                    Some("pdb.verify_index returned no verification checks".into()),
                );
            }
            let healthy = checks.iter().all(|check| check.passed);
            let error = (!healthy).then(|| {
                checks
                    .iter()
                    .filter(|check| !check.passed)
                    .map(|check| check.details.as_deref().unwrap_or(&check.name))
                    .collect::<Vec<_>>()
                    .join("; ")
            });
            Bm25IndexHealth {
                name,
                state: if healthy { "healthy" } else { "damaged" },
                repaired: false,
                checks,
                error,
            }
        }
        Err(error) => verification_error(name, &error),
    }
}

fn verification_error(name: &'static str, error: &postgres::Error) -> Bm25IndexHealth {
    let state = if error
        .code()
        .is_some_and(|code| CORRUPTION_SQLSTATES.contains(&code.code()))
    {
        "damaged"
    } else {
        "error"
    };
    index_health(name, state, Some(postgres_errors::message(error)))
}

fn index_health(name: &'static str, state: &'static str, error: Option<String>) -> Bm25IndexHealth {
    Bm25IndexHealth {
        name,
        state,
        repaired: false,
        checks: Vec::new(),
        error,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn degraded_text_includes_repair_command_and_error() {
        let health = Bm25Health {
            healthy: false,
            repair_command: REPAIR_COMMAND,
            indexes: vec![index_health(
                REQUIRED_INDEXES[0],
                "damaged",
                Some("invalid chunk style tag: 254".into()),
            )],
        };

        let text = health.render_text();
        assert!(text.contains("invalid chunk style tag: 254"));
        assert!(text.contains(REPAIR_COMMAND));
    }
}
