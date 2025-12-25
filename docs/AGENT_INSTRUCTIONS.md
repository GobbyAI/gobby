# Agent Instructions for Gobby Development

**Welcome, AI Agent.**

This document defines the standard operating procedures for working within the Gobby environment. You are expected to follow these workflows to ensure continuity, reliability, and efficient collaboration with the user and future agent sessions.

## Core Philosophy: "If it's not a task, it didn't happen."

Gobby uses a persistent task tracking system backed by SQLite. Your memory is ephemeral; the task database is permanent.

* **Do not rely on chat history** for long-term state.
* **Do not use loose markdown files** for tracking active work (unless explicitly requested as an artifact).
* **Always use the `gobby-tasks` tools** to read, create, and update tasks.

## Session Workflow

### 1. The "Start of Session" Protocol

When you begin a new session or receive a new request:

1. **Check for existing context:**
    * Call `list_tools(server="gobby-tasks")` to confirm the task tools are available.
    * Call `list_ready_tasks` to see what is queued and unblocked.
    * Call `get_task` if the user refers to a specific task by ID (e.g., "work on gt-1234").

2. **Define your work:**
    * **If the request is new:** Create a new task immediately.

        ```python
        create_task(title="Refactor auth middleware", priority=1)
        ```

    * **If the request is huge:** Break it down into subtasks.

        ```python
        parent = create_task(title="Implement User Profiles")
        create_task(title="Database schema", parent_task_id=parent['id'])
        create_task(title="API endpoints", parent_task_id=parent['id'], blocks=[parent['id']]) # Logic: Child blocks parent
        ```

3. **Link to the current session:**
    * Use `link_task_to_session` to associate the task you are working on with the current session ID (if available in your context). This builds a knowledge graph of *who* did *what* *when*.

### 2. The Execution Loop

While working:

* **Update status:** Mark tasks as `in_progress` when you start.
* **Log discoveries:** If you find a bug unrelated to your current task, create a new task for it (don't get distracted).

    ```python
    create_task(title="Fix memory leak in parser", task_type="bug", priority=2)
    ```

* **Manage dependencies:** If you get blocked, find the blocking task ID and record it.

    ```python
    add_dependency(task_id="my-current-task", depends_on="blocking-task-id", dep_type="blocks")
    ```

### 3. "Landing the Plane" (End of Session)

Before you finish or hand off:

1. **Review your active tasks:**
    * Did you finish? Call `close_task`.
    * Did you get stuck? Update the description with your findings.
2. **Ensure clean state:**
    * Do not leave tasks in `in_progress` if you are not actually working on them anymore.
    * If you created temporary files, clean them up or document them.
3. **Handoff:**
    * Your final message or summary should reference the Task IDs (`gt-xxxx`) you worked on.

## Tool Usage Best Practices

* **`create_task`**: Be descriptive with titles. "Fix bug" is bad. "Fix NullPointerException in login flow" is good.
* **`list_ready_tasks`**: Use this instead of `list_tasks(status='open')` when deciding what to do next. It filters out tasks that are blocked by other open tasks.
* **`add_label`**: Use labels for categorization (e.g., `frontend`, `backend`, `urgent`, `cleanup`).

## Troubleshooting

* **Tool not found?** If `create_task` fails, the Gobby daemon might be down. Ask the user to check `gobby status`.
* **Duplicate tasks?** Use `search_tasks` (or `list_tasks` with filters) before creating new ones to avoid duplicates.

---
*End of Instructions. Go build something great.*
