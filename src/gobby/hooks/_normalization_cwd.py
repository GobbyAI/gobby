"""Parent-shell directory state across exclusive and fallthrough branches."""

import re
from dataclasses import dataclass, field

from gobby.hooks._normalization_compound import case_arm_tokens
from gobby.hooks._normalization_env import UNRESOLVED_DIRECTORY
from gobby.hooks._normalization_shell import ShellToken, _apply_cd


@dataclass
class _Branch:
    kind: str
    cwd: str | None
    changed: bool
    altered: bool = False


@dataclass
class ShellDirectoryState:
    cwd: str | None
    changed: bool = False
    branches: list[_Branch] = field(default_factory=list)

    def compound_tokens(
        self, tokens: list[ShellToken], gap: str, case_depth: int, pending: bool
    ) -> tuple[list[ShellToken], int, bool]:
        self.case_boundary(tokens, gap)
        tokens, case_depth, pending = case_arm_tokens(
            tokens, case_depth=case_depth, new_arm=pending or ";;" in gap or ";&" in gap
        )
        while tokens:
            tokens = self.conditional_tokens(tokens)
            if not tokens or tokens[0].quoted or tokens[0].value not in {"case", "esac"}:
                break
            self.case_boundary(tokens, "")
            tokens, case_depth, pending = case_arm_tokens(
                tokens, case_depth=case_depth, new_arm=False
            )
        return tokens, case_depth, pending

    def case_boundary(self, tokens: list[ShellToken], gap: str) -> None:
        """Snapshot case entry, reset exclusive arms, and join their exit states."""
        if not tokens:
            return
        first = tokens[0]
        if not first.quoted and first.value == "esac":
            self._close("case")
        elif not first.quoted and first.value == "case":
            self.branches.append(_Branch("case", self.cwd, self.changed))
        elif re.search(r";;(?!&)", gap):
            self._restore("case")

    def conditional_tokens(self, tokens: list[ShellToken]) -> list[ShellToken]:
        """Remove executable if/elif prefixes while restoring branch entry cwd."""
        while tokens and not tokens[0].quoted:
            word = tokens[0].value
            if word == "if":
                self.branches.append(_Branch("if", self.cwd, self.changed))
            elif word in {"else", "elif"}:
                self._restore("if")
            elif word == "fi":
                self._close("if")
            elif word == "then":
                pass
            else:
                break
            tokens = tokens[1:]
        return tokens

    def change(self, target: str | None) -> None:
        self.cwd = _apply_cd(self.cwd, target) if target is not None else UNRESOLVED_DIRECTORY
        self.changed = True
        for branch in self.branches:
            branch.altered = True

    def _restore(self, kind: str) -> None:
        if self.branches and self.branches[-1].kind == kind:
            branch = self.branches[-1]
            self.cwd, self.changed = branch.cwd, branch.changed

    def _close(self, kind: str) -> None:
        if self.branches and self.branches[-1].kind == kind:
            branch = self.branches.pop()
            self.cwd = UNRESOLVED_DIRECTORY if branch.altered else branch.cwd
            self.changed = branch.changed or branch.altered
