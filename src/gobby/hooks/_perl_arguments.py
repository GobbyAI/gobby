"""Perl switch parsing: which file operands an inline ``perl -i`` loop edits in place."""

from string import hexdigits, octdigits

# The value is the rest of the switch cluster, or the next word when the cluster ends.
_SEPARABLE_VALUE_SWITCHES = frozenset("eEI")
# The value is the rest of the switch cluster, possibly empty; -i's value is the backup suffix.
_ATTACHED_VALUE_SWITCHES = frozenset("CdDFimMVx")
_INLINE_PROGRAM_SWITCHES = frozenset("eE")
# -p and -n loop over the operands; -a and -F imply -n.
_LOOP_SWITCHES = frozenset("pnaF")
# Compile-only and terminal modes run no loop; -x changes directory before the program runs.
_OPAQUE_SWITCHES = frozenset("chvVx")


def perl_inplace_operands(words: list[str]) -> list[str] | None:
    """Return the operands an inline ``perl -i`` loop edits, or None when perl stays opaque.

    Only ``-i`` with an implicit ``-p``/``-n`` loop over an inline ``-e``/``-E`` program
    names its writes. A script file, a bare ``-i``, compile-only and terminal modes and
    ``-x`` give no writer evidence. Switches end at ``--`` or the first non-switch word.
    """
    seen: set[str] = set()
    index = 1
    while index < len(words):
        word = words[index]
        if word == "--":
            index += 1
            break
        if not word.startswith("-") or word == "-":
            break
        cluster = word[1:]
        pos = 0
        while pos < len(cluster):
            switch = cluster[pos]
            pos += 1
            seen.add(switch)
            if switch in _SEPARABLE_VALUE_SWITCHES:
                if pos == len(cluster):
                    index += 1
                break
            if switch in _ATTACHED_VALUE_SWITCHES:
                break
            if switch == "0" and cluster[pos : pos + 1] == "x":
                pos = _skip(cluster, pos + 1, hexdigits)
            elif switch in "0l":
                pos = _skip(cluster, pos, octdigits)
        index += 1
    if (
        "i" in seen
        and seen & _INLINE_PROGRAM_SWITCHES
        and seen & _LOOP_SWITCHES
        and not seen & _OPAQUE_SWITCHES
    ):
        return words[index:]
    return None


def _skip(cluster: str, pos: int, digits: str) -> int:
    while pos < len(cluster) and cluster[pos] in digits:
        pos += 1
    return pos
