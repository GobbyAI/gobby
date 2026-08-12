/// Return the one baseline statement allowed during a single-hop refresh.
#[cfg(any(feature = "postgres", test))]
pub(crate) fn baseline_refresh_statement(statement: &str) -> Option<String> {
    let body = statement_body(statement);
    let refreshes_schema = body.trim_end()
        == "GRANT SELECT(id,revision) ON TABLE config_state TO gobby_gcode_capability";
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
            GRANT SELECT(id,revision) ON TABLE config_state TO gobby_gcode_capability";
        let misleading =
            "GRANT SELECT(id,revision) ON TABLE config_state TO gobby_gcode_capability_extra";

        assert!(baseline_refresh_statement(refreshable).is_some());
        assert!(baseline_refresh_statement(misleading).is_none());
    }

    #[test]
    fn rejects_broader_config_state_grants() {
        assert!(
            baseline_refresh_statement(
                "GRANT SELECT ON TABLE config_state TO gobby_gcode_capability"
            )
            .is_none()
        );
        assert!(
            baseline_refresh_statement(
                "GRANT SELECT(id,revision,secret) ON TABLE config_state TO gobby_gcode_capability"
            )
            .is_none()
        );
        assert!(
            baseline_refresh_statement(
                "GRANT SELECT(id,revision) ON TABLE config_state TO gobby_gcode_capability \
                 WITH GRANT OPTION"
            )
            .is_none()
        );
        assert!(
            baseline_refresh_statement(
                "GRANT SELECT(id,revision) ON TABLE config_state TO gobby_gcode_capability"
            )
            .is_some()
        );
    }
}
