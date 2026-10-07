-- Community refresh can skip resolver/graph/Leiden work for unchanged inputs.
-- NULL forces one rebuild after migration; replacement writes both signatures.
ALTER TABLE code_indexed_project_states ADD COLUMN community_input_digest text;
