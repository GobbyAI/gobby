-- gobby:non-transactional
-- Validate the widened source constraint outside the transactional schema batch.

ALTER TABLE recall_usefulness
    VALIDATE CONSTRAINT recall_usefulness_label_source_check;
