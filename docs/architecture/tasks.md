# Task Management

Gobby includes a native task tracking system designed for AI-assisted development. It helps you keep track of what you're working on, manages dependencies, and provides context to the AI agent.

## Core Concepts

* **Task**: A unit of work (e.g., "Implement login page", "Refactor database").
* **Epic**: A large task that groups smaller tasks (e.g., "Authentication System").
* **Context**: The "active" task provides context to the AI agent, so it knows the current objective.
* **Sync**: Tasks are stored in `.gobby/tasks.jsonl` and can be synced with git.

## CLI Usage

The `gobby tasks` command group manages tasks.

### Listing Tasks

```bash
gobby tasks list
```

Shows all open tasks.

* `--status <status>`: Filter by status (open, in_progress, completed, blocked).
* `--assignee <person>`: Filter by assignee.
* `--ready`: Show only ready tasks (open with no blocking dependencies).
* `--limit <n>`: Maximum tasks to show.
* `--json`: Output as JSON.

### Creating Tasks

```bash
gobby tasks create "Fix login bug"
```

* `-d, --description "Details..."`: Add a description.
* `-p, --priority <1-3>`: Set priority (1=High, 3=Low).
* `--type <task|epic|bug>`: Set task type.

### Managing Dependencies

You can mark tasks as blocking others.

```bash
# Task B blocks Task A
gobby tasks block <task_A_id> <task_B_id>
```

When listing tasks, blocked tasks are visually distinguished.

### Context & Hooks

When you work on a task, you can tell Gobby to focus on it.

```bash
gobby tasks start <task_id>
```

This sets the task as "active". If you have Gobby Hooks installed (`gobby install --hooks`), the active task's title and ID are automatically injected into your AI session context (Claude Code, Gemini, etc.).

### Git Integration

To keep tasks in sync with your code:

1. **Install Hooks**:

    ```bash
    gobby install --hooks
    ```

    This installs git hooks that:
    * **Pre-commit**: Exports tasks to `.gobby/tasks.jsonl` and stages the file.
    * **Post-merge**: Imports tasks from `.gobby/tasks.jsonl` after a pull.

2. **Manual Sync**:

    ```bash
    gobby tasks sync --export  # Save DB to file
    gobby tasks sync --import  # Load file to DB
    ```

## MCP Tools

If you are using an AI agent (like Claude Desktop or Gobby's own agent), it can manage tasks for you using these tools:

* `create_task`: Create a new task.
* `update_task`: Update status, title, etc.
* `list_ready_tasks`: Find work that is unblocked.
* `get_task_context`: Get details about the current active task.

## Data Storage

Tasks are stored locally in `~/.gobby/db.sqlite` and project-specifcally in `.gobby/tasks.jsonl` (when synced).
