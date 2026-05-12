"""Constants for the tool proxy service."""

PROXY_NAMESPACE = "gobby"

SERVER_SUGGESTIONS = {
    # Workflows subsystems (rules, pipelines, variables all live under gobby-workflows)
    "gobby-pipelines": "gobby-workflows",
    "gobby-pipeline": "gobby-workflows",
    "gobby-rules": "gobby-workflows",
    "gobby-rule": "gobby-workflows",
    "gobby-variables": "gobby-workflows",
    "gobby-variable": "gobby-workflows",
    # Singular -> plural
    "gobby-task": "gobby-tasks",
    "gobby-session": "gobby-sessions",
    "gobby-agent": "gobby-agents",
    "gobby-workflow": "gobby-workflows",
    "gobby-skill": "gobby-skills",
    "gobby-worktree": "gobby-worktrees",
    "gobby-clone": "gobby-clones",
    "gobby-profile": "gobby-profiles",
    # Scheduler aliases -> gobby-cron
    "gobby-scheduler": "gobby-cron",
    "gobby-schedule": "gobby-cron",
}
