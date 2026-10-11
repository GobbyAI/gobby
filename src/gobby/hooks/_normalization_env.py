"""Parse transparent env wrappers and their command-local directory changes."""


def env_command(parts: list[str]) -> tuple[list[str], tuple[str, ...]]:
    """Return the command and final chdir operand, preserving unknown wrappers."""
    if parts[:1] != ["env"]:
        return parts, ()
    index = 1
    directories: list[str] = []
    while index < len(parts):
        word = parts[index]
        if word == "--":
            index += 1
            break
        if word in {"-C", "--chdir", "-u", "--unset"}:
            if index + 1 >= len(parts):
                return parts, ()
            if word in {"-C", "--chdir"}:
                directories = [parts[index + 1]]
            index += 2
        elif word.startswith("--chdir=") or word.startswith("-C"):
            directories = [word.split("=", 1)[1] if "=" in word else word[2:]]
            index += 1
        elif word in {"-i", "--ignore-environment", "-"} or word.startswith("--unset="):
            index += 1
        elif word.startswith("-u"):
            index += 1
        elif word.startswith("-"):
            return parts, ()
        elif "=" in word and word.split("=", 1)[0].replace("_", "a").isalnum():
            index += 1
        else:
            break
    return parts[index:], tuple(directories)


def env_directory_operands(parts: list[str]) -> tuple[str, ...]:
    """Collect chdir operands through nested transparent command/env prefixes."""
    directories: list[str] = []
    remaining = parts
    while remaining:
        word = remaining[0]
        if word == "command" or ("=" in word and word.split("=", 1)[0].replace("_", "a").isalnum()):
            remaining = remaining[1:]
            continue
        unwrapped, changes = env_command(remaining)
        if unwrapped == remaining:
            break
        directories.extend(changes)
        remaining = unwrapped
    return tuple(directories)
