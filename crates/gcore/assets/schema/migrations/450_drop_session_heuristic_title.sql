-- Session titles no longer have a heuristic source (#22739); nothing reads or
-- writes the column 436 added. reasoning_effort from 436 stays.
ALTER TABLE sessions DROP COLUMN heuristic_title;
