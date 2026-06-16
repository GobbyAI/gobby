-- Allow memory dream snapshots to audit promote-to-global actions.

UPDATE memory_dream_snapshots
   SET action = 'review'
 WHERE action NOT IN ('keep', 'delete', 'refresh', 'merge', 'supersede', 'review', 'promote');

ALTER TABLE memory_dream_snapshots
    DROP CONSTRAINT IF EXISTS memory_dream_snapshots_action_check;

ALTER TABLE memory_dream_snapshots
    ADD CONSTRAINT memory_dream_snapshots_action_check
    CHECK (action IN ('keep', 'delete', 'refresh', 'merge', 'supersede', 'review', 'promote'));
