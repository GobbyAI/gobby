-- gobby:non-transactional
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_recall_usefulness_request_source_protocol
ON recall_usefulness(recall_request_id, label_source, judge_protocol_version);
