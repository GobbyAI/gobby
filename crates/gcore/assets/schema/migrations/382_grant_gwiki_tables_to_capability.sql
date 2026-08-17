-- Grant interactive/gcode capability roles access to attached wiki tables.
-- Wiki tables are created by gwiki, not the gcore baseline. Absent tables are
-- a no-op so fresh lineages and hubs without wiki stay valid.
DO $grant_gwiki$
DECLARE
    wiki_table text;
BEGIN
    FOREACH wiki_table IN ARRAY ARRAY[
        'gwiki_documents',
        'gwiki_chunks',
        'gwiki_links',
        'gwiki_sources',
        'gwiki_ingestions'
    ]
    LOOP
        IF to_regclass(wiki_table) IS NULL THEN
            CONTINUE;
        END IF;
        EXECUTE format(
            'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %I TO gobby_gcode_capability',
            wiki_table
        );
    END LOOP;
END
$grant_gwiki$;
