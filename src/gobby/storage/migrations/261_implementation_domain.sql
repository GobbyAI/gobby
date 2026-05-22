ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS implementation_domain TEXT CHECK (
        implementation_domain IS NULL
        OR implementation_domain IN ('backend', 'frontend', 'fullstack')
    );

UPDATE tasks
   SET implementation_domain = CASE
       WHEN assigned_agent = 'frontend-developer' THEN 'frontend'
       WHEN assigned_agent = 'fullstack-developer' THEN 'fullstack'
       WHEN assigned_agent = 'backend-developer' THEN 'backend'
       WHEN (
           COALESCE(title, '') || ' ' || COALESCE(description, '')
       ) ~* '(frontend|react|vue|svelte|ui|css|component|browser|client|playwright|vite|webpack|storybook|accessibility)'
           THEN 'frontend'
       ELSE 'backend'
   END
 WHERE category = 'code'
   AND closed_at IS NULL
   AND implementation_domain IS NULL;
