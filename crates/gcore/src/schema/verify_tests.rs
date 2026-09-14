use super::{normalize_function_definition, normalize_function_sql};

#[test]
fn function_normalization_preserves_quoted_data_and_identifier_boundaries() {
    let source = "SELECT tenant.sample, \"tenant\".sample, tenant_agent_auth.check(), \
                  other_tenant.sample, étenant.sample, 'tenant.sample', E'it\\'s tenant.sample', \
                  $literal$tenant.sample$literal$, \"tenant.sample\";";
    assert_eq!(
        normalize_function_sql(source, "tenant", "tenant_agent_auth", true),
        "SELECT $schema.sample, $schema.sample, $auth_schema.check(), \
                  other_tenant.sample, étenant.sample, 'tenant.sample', E'it\\'s tenant.sample', \
                  $literal$tenant.sample$literal$, \"tenant.sample\";"
    );
}

#[test]
fn function_comments_inside_literals_are_semantic() {
    let source = "\n  -- harmless comment\nSELECT 'first\n-- retained string\nlast', \
                  $literal$first\n-- retained dollar string\nlast$literal$;\n";
    let normalized = normalize_function_sql(source, "public", "gobby_agent_auth", true);
    assert_eq!(normalized, source.replace("  -- harmless comment\n", ""));
    assert_ne!(
        normalized,
        normalize_function_sql(
            &source.replace("-- retained string", "-- changed string"),
            "public",
            "gobby_agent_auth",
            true
        )
    );
}

#[test]
fn unicode_dollar_quotes_preserve_schema_names_and_comment_text() {
    let source = "SELECT $étiquette$public.sample\n-- literal text\n$étiquette$;";
    assert_eq!(
        normalize_function_sql(source, "public", "gobby_agent_auth", true),
        source
    );
    let changed = source.replace("-- literal text", "-- different text");
    assert_ne!(
        normalize_function_sql(&changed, "public", "gobby_agent_auth", true),
        source
    );
}

#[test]
fn argument_default_cannot_impersonate_the_function_body_wrapper() {
    let source = "CREATE OR REPLACE FUNCTION public.sample(value text DEFAULT 'first\nAS $fake$public.sample$fake$')\n RETURNS text\n LANGUAGE sql\nAS $function$\n SELECT value;\n$function$\n";
    let expected = source.replacen("FUNCTION public.sample", "FUNCTION $schema.sample", 1);
    assert_eq!(
        normalize_function_definition(source, "public", "gobby_agent_auth", &Default::default()),
        expected.trim_end_matches('\n')
    );
}

#[test]
fn closing_body_delimiter_can_follow_sql_or_indentation() {
    for body in [
        " SELECT id FROM public.sample ",
        "\n SELECT id FROM public.sample;\n    ",
    ] {
        let source = format!(
            "CREATE OR REPLACE FUNCTION public.sample()\n RETURNS integer\n LANGUAGE sql\nAS $function${body}$function$\n"
        );
        assert_eq!(
            normalize_function_definition(
                &source,
                "public",
                "gobby_agent_auth",
                &Default::default()
            ),
            source.replace("public.", "$schema.").trim_end_matches('\n')
        );
    }
}

#[test]
fn function_definition_normalizes_only_outer_source_and_search_path() {
    let source = "CREATE OR REPLACE FUNCTION tenant_agent_auth.sample()\n RETURNS text\n \
LANGUAGE sql\n SECURITY DEFINER\n SET search_path TO 'tenant_agent_auth', 'pg_temp'\n\
AS $function$\n  -- removable\n SELECT $text$tenant.sample\n-- data$text$ || 'tenant.sample';\n\
$function$\n";
    let expected = "CREATE OR REPLACE FUNCTION $auth_schema.sample()\n RETURNS text\n \
LANGUAGE sql\n SECURITY DEFINER\n SET search_path TO '$auth_schema', 'pg_temp'\n\
AS $function$\n SELECT $text$tenant.sample\n-- data$text$ || 'tenant.sample';\n\
$function$";
    assert_eq!(
        normalize_function_definition(source, "tenant", "tenant_agent_auth", &Default::default()),
        expected
    );
}
