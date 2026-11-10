-- The status line keeps its printable shape at the column as well as at the writer: no
-- C0, DEL or C1 control character may be stored, so a value that reaches the row by any
-- route cannot draw an escape sequence into the sidebar. Replaces the 493 check, which
-- covered only the blank and length rules.
ALTER TABLE sessions DROP CONSTRAINT IF EXISTS sessions_status_line_check;
ALTER TABLE sessions ADD CONSTRAINT sessions_status_line_check CHECK (
    status_line IS NULL OR (
        btrim(status_line) <> ''
        AND length(status_line) <= 120
        AND status_line !~ '[\x01-\x1F\x7F-\x9F]'
    )
);
