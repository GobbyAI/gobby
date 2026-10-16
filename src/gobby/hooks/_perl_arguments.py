"""Perl switch parsing: which file operands a ``perl -i`` command edits in place."""

from string import hexdigits, octdigits

# The value is the rest of the switch cluster, or the next word when the cluster ends.
_SEPARABLE_VALUE_SWITCHES = frozenset("eEI")
# The value is the rest of the switch cluster, possibly empty; -i's value is the backup suffix.
_ATTACHED_VALUE_SWITCHES = frozenset("CdDFimMx")


def perl_inplace_operands(words: list[str]) -> list[str] | None:
    """Return the operands ``perl`` edits in place, or None when it has no ``-i`` switch.

    Switches end at ``--`` or the first non-switch word. Without ``-e``/``-E`` that
    word is the script and the operands follow it, so a ``-i`` after the script is
    the script's argument.
    """
    inplace = program = False
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
            if switch in _SEPARABLE_VALUE_SWITCHES:
                program |= switch != "I"
                if pos == len(cluster):
                    index += 1
                break
            if switch in _ATTACHED_VALUE_SWITCHES:
                inplace |= switch == "i"
                break
            if switch == "0" and cluster[pos : pos + 1] == "x":
                pos = _skip(cluster, pos + 1, hexdigits)
            elif switch in "0l":
                pos = _skip(cluster, pos, octdigits)
        index += 1
    if not inplace:
        return None
    operands = words[index:]
    return operands if program else operands[1:]


def _skip(cluster: str, pos: int, digits: str) -> int:
    while pos < len(cluster) and cluster[pos] in digits:
        pos += 1
    return pos
