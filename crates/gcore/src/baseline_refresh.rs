/// The one baseline statement allowed during a single-hop refresh: the
/// 24-hour principal-lifetime guard superseding the one-hour definition.
#[cfg(any(feature = "postgres", test))]
const REFRESH_STATEMENT: &str = "\
CREATE OR REPLACE FUNCTION gobby_agent_auth.enforce_principal_lifetime()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
BEGIN
    IF NEW.expires_at <= NEW.issued_at THEN
        RAISE EXCEPTION 'managed principal expiry must follow issuance'
            USING ERRCODE = '22023';
    END IF;
    IF NEW.expires_at > NEW.issued_at + INTERVAL '24 hours' THEN
        RAISE EXCEPTION 'managed principal lifetime exceeds 24 hours'
            USING ERRCODE = '22023';
    END IF;
    RETURN NEW;
END
$function$";

/// Return the one baseline statement allowed during a single-hop refresh.
#[cfg(any(feature = "postgres", test))]
pub(crate) fn baseline_refresh_statement(statement: &str) -> Option<String> {
    let body = statement_body(statement);
    let refreshes_schema = body.trim_end() == REFRESH_STATEMENT;
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
    use super::{REFRESH_STATEMENT, baseline_refresh_statement};

    #[test]
    fn ignores_leading_comments_and_requires_exact_statement() {
        let refreshable = format!("-- retained context\n{REFRESH_STATEMENT}");
        let misleading = REFRESH_STATEMENT.replace("24 hours", "240 hours");

        assert!(baseline_refresh_statement(&refreshable).is_some());
        assert!(baseline_refresh_statement(&misleading).is_none());
    }

    #[test]
    fn rejects_superseded_and_adjacent_statements() {
        // The predecessor's one-hour definition must stay out of the refresh set.
        let one_hour = REFRESH_STATEMENT
            .replace("INTERVAL '24 hours'", "INTERVAL '1 hour'")
            .replace("exceeds 24 hours", "exceeds one hour");
        assert!(baseline_refresh_statement(&one_hour).is_none());
        // The previous hop's refresh statement is now part of the predecessor set.
        assert!(
            baseline_refresh_statement(
                "ALTER TABLE embedding_projection_changes ALTER COLUMN sequence SET CACHE 1"
            )
            .is_none()
        );
        // Trailing statements must not smuggle extra SQL into the refresh.
        let stacked = format!("{REFRESH_STATEMENT};\nDROP TABLE gobby_agent_auth.daemon_registry");
        assert!(baseline_refresh_statement(&stacked).is_none());
        assert!(baseline_refresh_statement(REFRESH_STATEMENT).is_some());
    }
}
