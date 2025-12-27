from gobby.storage.memories import Memory
from gobby.storage.skills import Skill


def build_memory_context(memories: list[Memory], skills: list[Skill]) -> str:
    """
    Build a formatted markdown context string from memories and skills.

    Args:
        memories: List of Memory objects to include
        skills: List of Skill objects to include

    Returns:
        Formatted markdown string wrapped in <project-memory> tags
    """
    if not memories and not skills:
        return ""

    parts = ["<project-memory>"]

    # Group memories by type
    context_memories = [m for m in memories if m.memory_type == "context"]
    pref_memories = [m for m in memories if m.memory_type == "preference"]
    pattern_memories = [m for m in memories if m.memory_type == "pattern"]
    fact_memories = [m for m in memories if m.memory_type == "fact"]

    # 1. Project Context
    if context_memories:
        parts.append("## Project Context\n")
        for mem in context_memories:
            parts.append(f"{mem.content}\n")
        parts.append("")

    # 2. Preferences
    if pref_memories:
        parts.append("## Preferences\n")
        for mem in pref_memories:
            # Strip bullet points if present to avoid double bullets
            content = mem.content.strip()
            if content.startswith("- "):
                content = content[2:]
            elif content.startswith("* "):
                content = content[2:]
            parts.append(f"- {content}")
        parts.append("")

    # 3. Patterns
    if pattern_memories:
        parts.append("## Patterns\n")
        for mem in pattern_memories:
            content = mem.content.strip()
            if content.startswith("- "):
                content = content[2:]
            elif content.startswith("* "):
                content = content[2:]
            parts.append(f"- {content}")
        parts.append("")

    # 4. Facts/Other
    if fact_memories:
        parts.append("## Facts\n")
        for mem in fact_memories:
            content = mem.content.strip()
            if content.startswith("- "):
                content = content[2:]
            elif content.startswith("* "):
                content = content[2:]
            parts.append(f"- {content}")
        parts.append("")

    # 5. Relevant Skills
    if skills:
        parts.append("## Relevant Skills\n")
        for skill in skills:
            parts.append(f"### {skill.name}")
            parts.append(f"{skill.instructions}\n")

    parts.append("</project-memory>")

    return "\n".join(parts)
