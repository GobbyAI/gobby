-- One line a session shows about its current work. The session sets and clears it for
-- itself through set_status_line; the daemon trims it and refuses anything past 120
-- characters, and the check keeps every stored value in that shape. Null means no line.
ALTER TABLE sessions ADD COLUMN status_line text CHECK (
    status_line IS NULL OR (btrim(status_line) <> '' AND length(status_line) <= 120)
);
