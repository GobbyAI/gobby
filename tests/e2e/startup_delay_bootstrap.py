"""Test entry point around the genuine daemon runner."""

import os
import runpy
from pathlib import Path
from typing import Any
from unittest.mock import patch

from gobby.skills import search

ENTERED_FILE = "skill-search-entered"
RELEASE_FILE = "skill-search-release"
RELEASED_FILE = "skill-search-released"


def main() -> None:
    home = Path(os.environ["GOBBY_HOME"])
    original = search.SkillSearch

    def delayed_skill_search(*args: Any, **kwargs: Any) -> search.SkillSearch:
        with (home / ENTERED_FILE).open("wb", buffering=0) as channel:
            channel.write(b"1")
        with (home / RELEASE_FILE).open("rb", buffering=0) as channel:
            if channel.read(1) != b"1":
                raise RuntimeError("Skill search delay did not receive its release signal")
        with (home / RELEASED_FILE).open("wb", buffering=0) as channel:
            channel.write(b"1")
        return original(*args, **kwargs)

    with patch.object(search, "SkillSearch", delayed_skill_search):
        runpy.run_module("gobby.runner", run_name="__main__")


if __name__ == "__main__":
    main()
