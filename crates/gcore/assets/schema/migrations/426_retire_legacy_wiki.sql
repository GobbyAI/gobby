-- gobby:destructive
-- Legacy wiki retirement (#21771). Apply to an installed hub only after the
-- retirement inventory, scoped recovery backup and writer shutdown are verified.
-- These five projections are owned by gwiki; canonical summaries/revisions,
-- handoffs, transcripts, memories, projects and code-index facts are independent.
-- Qualify every target to the runner's schema: IF EXISTS must never fall through
-- search_path into another schema. RESTRICT refuses unrecorded outside consumers.
DO $retire_wiki$
BEGIN
    EXECUTE format(
        'DROP TABLE IF EXISTS %1$I.gwiki_chunks, %1$I.gwiki_links, '
        '%1$I.gwiki_sources, %1$I.gwiki_ingestions, %1$I.gwiki_documents RESTRICT',
        current_schema()
    );
END
$retire_wiki$;
