-- Build profile plan-enhancement round target. Mirrors the build_profiles
-- delivery_mode/unattended overlay fields: a per-profile default round count
-- that the build-option overlay reads (explicit invocation, including explicit
-- 0, wins via the BuildOptions plan_enhancement_rounds_explicit marker).
ALTER TABLE build_profiles
    ADD COLUMN IF NOT EXISTS plan_enhancement_rounds INTEGER NOT NULL DEFAULT 0
        CHECK (plan_enhancement_rounds >= 0);
