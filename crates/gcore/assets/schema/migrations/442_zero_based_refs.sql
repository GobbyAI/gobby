-- Refs are letterless, left-anchored, and zero-based now (`0:1:2:1`), and the
-- lowest free ref in every scope starts at 0. Two passes per table keep the
-- UNIQUE (parent, ref) constraints satisfied while a whole scope shifts down.
UPDATE machines SET ref = -ref WHERE ref IS NOT NULL;
UPDATE machines SET ref = -ref - 1 WHERE ref IS NOT NULL;
UPDATE workspaces SET ref = -ref;
UPDATE workspaces SET ref = -ref - 1;
UPDATE workspace_tabs SET ref = -ref;
UPDATE workspace_tabs SET ref = -ref - 1;
UPDATE workspace_panes SET ref = -ref;
UPDATE workspace_panes SET ref = -ref - 1;
