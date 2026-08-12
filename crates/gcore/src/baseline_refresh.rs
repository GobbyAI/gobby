/// Return the one baseline statement allowed during a single-hop refresh.
#[cfg(any(feature = "postgres", test))]
pub(crate) fn baseline_refresh_statement(statement: &str) -> Option<String> {
    let body = statement_body(statement);
    let refreshes_schema = body.trim_end()
        == "ALTER TABLE embedding_projection_changes ALTER COLUMN sequence SET CACHE 1";
    refreshes_schema.then(|| statement.to_owned())
}

/// Strip leading SQL comments before comparing a baseline statement.
#[cfg(any(feature = "postgres", test))]
pub(crate) fn statement_body(mut statement: &str) -> &str {
    loop {
        statement = statement.trim_start();
        if let Some(comment) = statement.strip_prefix("--") {
            statement = comment
                .find('\n')
                .map_or("", |newline| &comment[newline + 1..]);
        } else if let Some(comment) = statement.strip_prefix("/*") {
            statement = comment.find("*/").map_or("", |end| &comment[end + 2..]);
        } else {
            return statement;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::baseline_refresh_statement;

    #[test]
    fn ignores_leading_comments_and_requires_exact_statement() {
        let refreshable = "-- retained context\n\
            ALTER TABLE embedding_projection_changes ALTER COLUMN sequence SET CACHE 1";
        let misleading =
            "ALTER TABLE embedding_projection_changes ALTER COLUMN sequence SET CACHE 10";

        assert!(baseline_refresh_statement(refreshable).is_some());
        assert!(baseline_refresh_statement(misleading).is_none());
    }

    #[test]
    fn rejects_other_identity_sequence_alters() {
        assert!(
            baseline_refresh_statement(
                "ALTER TABLE embedding_projection_changes ALTER COLUMN sequence SET CACHE 32"
            )
            .is_none()
        );
        assert!(
            baseline_refresh_statement(
                "ALTER TABLE embedding_projection_changes ALTER COLUMN sequence RESTART WITH 1"
            )
            .is_none()
        );
        assert!(
            baseline_refresh_statement(
                "ALTER TABLE embedding_projection_changes ALTER COLUMN sequence SET CACHE 1; \
                 ALTER TABLE embedding_projection_changes ALTER COLUMN sequence RESTART WITH 1"
            )
            .is_none()
        );
        assert!(
            baseline_refresh_statement(
                "ALTER TABLE embedding_projection_changes ALTER COLUMN sequence SET CACHE 1"
            )
            .is_some()
        );
    }
}
