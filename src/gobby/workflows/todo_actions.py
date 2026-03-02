"""Todo file workflow actions.

Extracted from actions.py as part of strangler fig decomposition.
These functions handle TODO.md file operations.
"""

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def write_todos(
    todos: list[str],
    filename: str = "TODO.md",
    mode: str = "w",
) -> dict[str, Any]:
    """Write todos to a file (default TODO.md).

    Args:
        todos: List of todo strings
        filename: Output filename (default: TODO.md)
        mode: "w" to overwrite, "append" to append

    Returns:
        Dict with todos_written count and file path, or error
    """
    try:
        formatted_todos = [f"- [ ] {todo}" for todo in todos]

        if mode == "append" and os.path.exists(filename):
            with open(filename, "a") as f:
                f.write("\n" + "\n".join(formatted_todos) + "\n")
        else:
            with open(filename, "w") as f:
                f.write("# TODOs\n\n" + "\n".join(formatted_todos) + "\n")

        return {"todos_written": len(todos), "file": filename}
    except Exception as e:
        logger.error(f"write_todos: Failed: {e}")
        return {"error": str(e)}


def mark_todo_complete(
    todo_text: str,
    filename: str = "TODO.md",
) -> dict[str, Any]:
    """Mark the first occurrence of a todo as complete in TODO.md.

    Args:
        todo_text: Text of the todo to mark complete
        filename: Todo file path (default: TODO.md)

    Returns:
        Dict with todo_completed boolean and text, or error
    """
    if not todo_text:
        return {"error": "Missing todo_text"}

    if not os.path.exists(filename):
        return {"error": "File not found"}

    try:
        with open(filename) as f:
            lines = f.readlines()

        updated = False
        new_lines = []
        for line in lines:
            if not updated and todo_text in line and "- [ ]" in line:
                new_lines.append(line.replace("- [ ]", "- [x]"))
                updated = True
            else:
                new_lines.append(line)

        if updated:
            with open(filename, "w") as f:
                f.writelines(new_lines)

        return {"todo_completed": updated, "text": todo_text}
    except Exception as e:
        logger.error(f"mark_todo_complete: Failed: {e}")
        return {"error": str(e)}


# --- ActionHandler-compatible wrappers ---
# These match the ActionHandler protocol: (context: ActionContext, **kwargs) -> dict | None


async def handle_write_todos(context: Any, **kwargs: Any) -> dict[str, Any] | None:
    """ActionHandler wrapper for write_todos."""
    return await asyncio.to_thread(
        write_todos,
        todos=kwargs.get("todos", []),
        filename=kwargs.get("filename", "TODO.md"),
        mode=kwargs.get("mode", "w"),
    )


async def handle_mark_todo_complete(context: Any, **kwargs: Any) -> dict[str, Any] | None:
    """ActionHandler wrapper for mark_todo_complete."""
    todo_text = kwargs.get("todo_text")
    if not todo_text:
        return {"error": "Missing required parameter: todo_text"}

    return await asyncio.to_thread(
        mark_todo_complete,
        todo_text,
        kwargs.get("filename", "TODO.md"),
    )
