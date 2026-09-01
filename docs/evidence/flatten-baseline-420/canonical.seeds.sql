INSERT INTO config_state (id, revision) VALUES (true, 0);

INSERT INTO projects (id, name, github_url, github_repo, linear_team_id, linear_project_id, linear_synced_at, linear_sync_enabled, deleted_at, created_at, updated_at) VALUES ('00000000-0000-0000-0000-000000000000', '_orphaned', NULL, NULL, NULL, NULL, NULL, false, NULL, NOW(), NOW());

INSERT INTO projects (id, name, github_url, github_repo, linear_team_id, linear_project_id, linear_synced_at, linear_sync_enabled, deleted_at, created_at, updated_at) VALUES ('00000000-0000-0000-0000-000000000001', '_migrated', NULL, NULL, NULL, NULL, NULL, false, NULL, NOW(), NOW());

INSERT INTO projects (id, name, github_url, github_repo, linear_team_id, linear_project_id, linear_synced_at, linear_sync_enabled, deleted_at, created_at, updated_at) VALUES ('00000000-0000-0000-0000-000000000002', '_global', NULL, NULL, NULL, NULL, NULL, false, NULL, NOW(), NOW());

INSERT INTO projects (id, name, github_url, github_repo, linear_team_id, linear_project_id, linear_synced_at, linear_sync_enabled, deleted_at, created_at, updated_at) VALUES ('00000000-0000-0000-0000-000000060887', '_personal', NULL, NULL, NULL, NULL, NULL, false, NULL, NOW(), NOW());

INSERT INTO task_stages_registry (name, display_label, description, category, default_agent, reviewer_agent, reviewer_agent_selector_json, review_policy, dispatch_type, dispatch_target, dispatch_inputs_json, position_hint, requires_human, is_terminal, default_max_work_attempts, default_max_review_rounds, bundled_hash, deleted_at, updated_at) VALUES ('architecture', 'Architecture', 'Cross-cutting design decisions and component shape.', 'design', 'architect', NULL, NULL, 'none', NULL, NULL, NULL, 30, false, false, 3, 5, 'd084b4acbf67c7012e577d2d386dc20ae45cbfebe347a58f3fbc89cef5038b2c', NULL, NOW());

INSERT INTO task_stages_registry (name, display_label, description, category, default_agent, reviewer_agent, reviewer_agent_selector_json, review_policy, dispatch_type, dispatch_target, dispatch_inputs_json, position_hint, requires_human, is_terminal, default_max_work_attempts, default_max_review_rounds, bundled_hash, deleted_at, updated_at) VALUES ('development', 'Development', 'Leaf implementation work; carries skill-backed TDD when required.', 'implementation', 'backend-developer', NULL, '{"rules": [{"category": "docs", "reviewer_agent": "doc-reviewer"}], "default": "qa-reviewer"}', 'required', NULL, NULL, NULL, 100, false, false, 3, 5, 'f8821338fc237ebc8abeb46a1e3303113e096593041a5dde768d0ff604221e54', NULL, NOW());

INSERT INTO task_stages_registry (name, display_label, description, category, default_agent, reviewer_agent, reviewer_agent_selector_json, review_policy, dispatch_type, dispatch_target, dispatch_inputs_json, position_hint, requires_human, is_terminal, default_max_work_attempts, default_max_review_rounds, bundled_hash, deleted_at, updated_at) VALUES ('epic_qa', 'Epic QA', 'Whole-epic review after every leaf is parked.', 'verification', 'epic-reviewer', 'epic-reviewer', NULL, 'required', NULL, NULL, NULL, 120, false, false, 3, 5, 'd5723c4eca017e49c46aa73ab3e331f1e37c98347af149f74863c5aa744145ae', NULL, NOW());

INSERT INTO task_stages_registry (name, display_label, description, category, default_agent, reviewer_agent, reviewer_agent_selector_json, review_policy, dispatch_type, dispatch_target, dispatch_inputs_json, position_hint, requires_human, is_terminal, default_max_work_attempts, default_max_review_rounds, bundled_hash, deleted_at, updated_at) VALUES ('expansion', 'Expansion', 'Decompose plan into manifest-backed leaf tasks.', 'implementation', NULL, 'expansion-qa', NULL, 'required', 'pipeline', 'expand-task', '{"task_id": "${{ task_id }}", "plan_file": "${{ artifacts.plan_file_path }}"}', 80, false, false, 3, 5, '7aea4dbb7119bcdab1cb5957239670ff4a68d2d78d50c6e3a7bda922fa3d9aa1', NULL, NOW());

INSERT INTO task_stages_registry (name, display_label, description, category, default_agent, reviewer_agent, reviewer_agent_selector_json, review_policy, dispatch_type, dispatch_target, dispatch_inputs_json, position_hint, requires_human, is_terminal, default_max_work_attempts, default_max_review_rounds, bundled_hash, deleted_at, updated_at) VALUES ('ideation', 'Ideation', 'Early problem framing; capture motivating questions and constraints.', 'discovery', 'analyst', NULL, NULL, 'none', NULL, NULL, NULL, 10, false, false, 3, 5, '30d0d059953b56f2cf9e809b42993be29df0da15598a38925b79a900a71e6331', NULL, NOW());

INSERT INTO task_stages_registry (name, display_label, description, category, default_agent, reviewer_agent, reviewer_agent_selector_json, review_policy, dispatch_type, dispatch_target, dispatch_inputs_json, position_hint, requires_human, is_terminal, default_max_work_attempts, default_max_review_rounds, bundled_hash, deleted_at, updated_at) VALUES ('merge', 'Merge', 'Land approved PR; resolve conflicts; close terminal task.', 'delivery', 'merge-orchestrator', NULL, NULL, 'none', NULL, NULL, NULL, 140, false, true, 3, 5, '636a12f800c8ceef76dd7fdea41baaa0b227fa3f178bf45e3802688e179ec6ef', NULL, NOW());

INSERT INTO task_stages_registry (name, display_label, description, category, default_agent, reviewer_agent, reviewer_agent_selector_json, review_policy, dispatch_type, dispatch_target, dispatch_inputs_json, position_hint, requires_human, is_terminal, default_max_work_attempts, default_max_review_rounds, bundled_hash, deleted_at, updated_at) VALUES ('planning', 'Planning', 'Implementation plan authoring (interactive or autonomous).', 'design', 'planner', 'plan-adversary', NULL, 'required', NULL, NULL, NULL, 50, false, false, 3, 5, 'b7d0a297c57659700b759ce3f3fd6cc5e4d66e8a2a18759358ec683f613f2b51', NULL, NOW());

INSERT INTO task_stages_registry (name, display_label, description, category, default_agent, reviewer_agent, reviewer_agent_selector_json, review_policy, dispatch_type, dispatch_target, dispatch_inputs_json, position_hint, requires_human, is_terminal, default_max_work_attempts, default_max_review_rounds, bundled_hash, deleted_at, updated_at) VALUES ('pr', 'Pull Request', 'Open/update PR, capture verdict, gate on external review.', 'delivery', 'merge-orchestrator', 'trajectory-monitor', NULL, 'required', NULL, NULL, NULL, 130, false, false, 3, 5, 'd39dab6946f4d93373497192cb1d751767cc593f7b45564d5aa2cb92d5a32aaa', NULL, NOW());

INSERT INTO task_stages_registry (name, display_label, description, category, default_agent, reviewer_agent, reviewer_agent_selector_json, review_policy, dispatch_type, dispatch_target, dispatch_inputs_json, position_hint, requires_human, is_terminal, default_max_work_attempts, default_max_review_rounds, bundled_hash, deleted_at, updated_at) VALUES ('prd', 'PRD', 'Productized requirements; bridges discovery and planning.', 'design', 'product-manager', NULL, NULL, 'none', NULL, NULL, NULL, 40, false, false, 3, 5, 'fd609d682a6fe7e807cfb487f301bfdb39f352bc8836b87e030bb0bbe7836360', NULL, NOW());

INSERT INTO task_stages_registry (name, display_label, description, category, default_agent, reviewer_agent, reviewer_agent_selector_json, review_policy, dispatch_type, dispatch_target, dispatch_inputs_json, position_hint, requires_human, is_terminal, default_max_work_attempts, default_max_review_rounds, bundled_hash, deleted_at, updated_at) VALUES ('research', 'Research', 'Targeted investigation; produce findings consumable by architecture/PRD.', 'discovery', 'researcher', NULL, NULL, 'none', NULL, NULL, NULL, 20, false, false, 3, 5, 'c18eb91008e5375fcc3395a220cf6bf7146cb5c1752f68daf848598a45857221', NULL, NOW());

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('architecture_doc', 'architecture', 1);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('architecture_doc', 'research', 0);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('bug', 'development', 0);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('bug', 'merge', 2);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('bug', 'pr', 1);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('chore', 'development', 0);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('chore', 'merge', 2);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('chore', 'pr', 1);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('epic', 'architecture', 2);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('epic', 'development', 6);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('epic', 'epic_qa', 7);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('epic', 'expansion', 5);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('epic', 'ideation', 0);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('epic', 'merge', 9);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('epic', 'planning', 4);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('epic', 'pr', 8);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('epic', 'prd', 3);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('epic', 'research', 1);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('feature', 'development', 2);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('feature', 'expansion', 1);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('feature', 'merge', 4);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('feature', 'planning', 0);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('feature', 'pr', 3);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('prd_doc', 'ideation', 0);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('prd_doc', 'prd', 1);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('refactor', 'development', 1);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('refactor', 'merge', 3);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('refactor', 'planning', 0);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('refactor', 'pr', 2);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('research_spike', 'ideation', 0);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('research_spike', 'prd', 2);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('research_spike', 'research', 1);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('simple_fix', 'development', 0);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('simple_fix', 'merge', 2);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('simple_fix', 'pr', 1);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('task', 'development', 0);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('task', 'merge', 2);

INSERT INTO task_type_default_stages (task_type, stage_name, "position") VALUES ('task', 'pr', 1);
