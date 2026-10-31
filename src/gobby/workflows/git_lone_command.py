"""The closed grammar of the git calls a base merge may spell, read from one raw command.

Base-merge admission and the authored-merge observer read this same grammar, so a merge
the observer credits is a merge the admission can also admit. The grammar is kept apart
from `base_merge_commits` so the observer does not import the merge admission's storage
and enforcement dependencies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PATH = r"(?!-)[A-Za-z0-9._/-]+"
_MESSAGE = r"[A-Za-z0-9 ._:#()/,-]+"
_GIT = rf"git(?: -C (?P<target>{_PATH}))? "
# The closed grammar. Each form must match the whole raw command text on one line.
_FORMS = tuple(
    re.compile(form)
    for form in (
        rf"{_GIT}(?P<args>merge(?: --no-ff)?(?: --no-edit)? {_PATH})",
        rf"{_GIT}(?P<args>merge --(?:continue|abort))",
        rf"{_GIT}(?P<args>add -- {_PATH}(?: {_PATH})*)",
        rf"{_GIT}(?P<args>commit --no-edit)",
        rf"{_GIT}(?P<args>commit(?: --no-edit)? -m) '(?P<message>{_MESSAGE})'",
    )
)


@dataclass(frozen=True)
class GitCall:
    """A git call the closed grammar admits: its `-C` path and its argument tokens."""

    target: str | None
    tokens: list[str]


def lone_git_command(command: str | None) -> GitCall | None:
    """The one git call a raw command spells in the closed grammar, or None.

    The whole text must fit one form. A second segment, a `cd`, a global option, a
    redirection, a quote, a comment, a heredoc or a substitution fails the match, so
    no shell reading decides admission. A `-C` path is the call's target.
    """
    if not isinstance(command, str):
        return None
    for form in _FORMS:
        match = form.fullmatch(command)
        if match is None:
            continue
        tokens = ["git", *(token for token in match["args"].split(" ") if token != "--")]
        message = match.groupdict().get("message")
        if message is not None:
            tokens.append(message)
        return GitCall(target=match["target"], tokens=tokens)
    return None
