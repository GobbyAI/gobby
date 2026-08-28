# Common rule patterns

## Common Patterns

### Block a specific command
```yaml
no-force-push:
  event: before_tool
  effect:
    type: block
    tools: [Bash]
    command_pattern: "git\\s+push\\s+.*--force"
    reason: "Force push is not allowed."
```

### Require a prerequisite
```yaml
require-tests-before-commit:
  event: before_tool
  when: "not variables.get('tests_passed', False)"
  effect:
    type: block
    tools: [Bash]
    command_pattern: "git\\s+commit"
    reason: "Run tests before committing."
```

### Count and gate
```yaml
count-attempts:
  event: turn_end
  priority: 10
  effect:
    type: set_variable
    variable: stop_attempts
    value: "variables.get('stop_attempts', 0) + 1"

block-after-threshold:
  event: turn_end
  priority: 50
  when: "variables.get('stop_attempts', 0) < 5"
  effect:
    type: block
    reason: "Complete your work before stopping."
```

### Inject context conditionally
```yaml
inject-tdd-reminder:
  event: turn_start
  when: "variables.get('enforce_tdd') and variables.get('task_claimed')"
  effect:
    type: inject_context
    template: |
      ## TDD Mode Active
      Write tests BEFORE implementation code.
```

### Observe memory stats on session start
```yaml
observe-memory-stats:
  event: session_start
  priority: 30
  effect:
    type: mcp_call
    server: gobby-memory
    tool: memory_stats
    background: true
```

---
