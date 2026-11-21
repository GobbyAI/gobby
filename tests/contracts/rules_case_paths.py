"""Loader checks for the path keys of a rule-pass corpus case.

A case names the engine paths it drives (``reaches``), the paths it is a control for
(``control_for``, with a ``twin_of`` that reaches them), the seam whose failure it proves
(``seam_of``, with ``faults``), the inline branches it pins by two of its own passes
(``branch_passes``) and, for a seam caller, the keys of its returned value that are a
harness-written log (``returned_spy_keys``). ``validate_case_paths`` checks one case and
``validate_corpus_paths`` checks what spans cases: twins and branch pairing. Every refusal
is a ``ValueError`` that names the case and the offending key.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from tests.contracts import rules_domain, rules_faults, rules_handler, rules_paths

HANDLER_ENTRY = rules_handler.HANDLER_ENTRY
ENTRIES = frozenset({"engine", HANDLER_ENTRY})
CALLER_KIND = "seam_caller"
PASS_KINDS = frozenset({"rule_pass", "handler_pass"})
# Keys that describe a case rather than feed its run; a differs_at path never names them.
_BOOKKEEPING_KEYS = frozenset({"expect"})


def _path(path_id: Any) -> Any:
    return rules_paths.PATHS.get(path_id) if isinstance(path_id, str) else None


def _is_fault_branch(path_id: Any) -> bool:
    path = _path(path_id)
    return isinstance(path, rules_paths.BranchPath) and path.fault


def _id_list(case_id: str, case: Mapping[str, Any], key: str) -> list[str]:
    value = case.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{case_id}: {key} must be a list of path ids, got {value!r}")
    unknown = [item for item in value if item not in rules_paths.PATHS]
    if unknown:
        raise ValueError(f"{case_id}: {key} names unknown path id(s) {unknown}")
    return list(value)


def _validate_entry(case_id: str, case: Mapping[str, Any]) -> None:
    entry = case.get("entry")
    if entry is not None and entry not in ENTRIES:
        raise ValueError(f"{case_id}: entry must be one of {sorted(ENTRIES)}, got {entry!r}")
    kind = case.get("kind")
    if kind == "handler_pass" and entry != HANDLER_ENTRY:
        raise ValueError(f"{case_id}: a handler_pass case needs entry {HANDLER_ENTRY!r}")
    if kind == "rule_pass" and entry == HANDLER_ENTRY:
        raise ValueError(f"{case_id}: a rule_pass case runs the engine entry, not {entry!r}")
    if "seed" in case and "domain" in case:
        raise ValueError(f"{case_id}: give seed or domain, not both")
    if kind == "handler_pass" and case.get("domain") is None:
        raise ValueError(f"{case_id}: a handler_pass case needs the domain it runs on")
    if kind != "handler_pass" and "domain" in case:
        raise ValueError(f"{case_id}: a domain is seeded for a handler_pass case only")
    if "domain" in case:
        rules_domain.validate_domain(case_id, case["domain"])
    if "requires" in case:
        rules_handler.validate_requires(case_id, case["requires"])


def _validate_coverage(case_id: str, case: Mapping[str, Any]) -> None:
    reaches = _id_list(case_id, case, "reaches")
    controls = _id_list(case_id, case, "control_for")
    branches = [pid for pid in controls if not isinstance(_path(pid), rules_paths.CallablePath)]
    if branches:
        raise ValueError(f"{case_id}: control_for names non-callable path(s) {branches}")
    both = sorted(set(reaches) & set(controls))
    if both:
        raise ValueError(f"{case_id}: {both} under both reaches and control_for")
    if controls and "twin_of" not in case:
        raise ValueError(f"{case_id}: control_for needs the twin_of that reaches the path")
    if "seam_of" in case:
        seam = case["seam_of"]
        if _path(seam) is None:
            raise ValueError(f"{case_id}: seam_of names an unknown path id {seam!r}")
        if seam in reaches or seam in controls:
            raise ValueError(f"{case_id}: seam_of {seam!r} is also under reaches or control_for")
        if "twin_of" not in case:
            raise ValueError(f"{case_id}: seam_of needs a twin_of without the fault")
        if not case.get("faults"):
            raise ValueError(f"{case_id}: seam_of needs the faults that degrade the seam")
    if "faults" in case:
        rules_faults.validate_faults(case_id, case["faults"])
        if "twin_of" not in case:
            raise ValueError(f"{case_id}: faults need a twin_of without the faults")
    unfaulted = [pid for pid in reaches if _is_fault_branch(pid)]
    if unfaulted and not case.get("faults"):
        raise ValueError(f"{case_id}: fault branch(es) {unfaulted} under reaches need faults")
    if "twin_of" in case:
        if not isinstance(case["twin_of"], str):
            raise ValueError(f"{case_id}: twin_of must name a case id, got {case['twin_of']!r}")
        differs_at = case.get("differs_at")
        if (
            not isinstance(differs_at, list)
            or not differs_at
            or not all(isinstance(path, str) and path for path in differs_at)
        ):
            raise ValueError(f"{case_id}: a twin needs differs_at, a non-empty list of paths")
    _validate_branch_passes(case_id, case, reaches)


def _validate_branch_passes(case_id: str, case: Mapping[str, Any], reaches: list[str]) -> None:
    pairs = case.get("branch_passes")
    if pairs is None:
        return
    if not isinstance(pairs, Mapping) or not pairs:
        raise ValueError(f"{case_id}: branch_passes must be a non-empty mapping")
    count = 1 + len(case.get("sequence", []))
    for path_id, pair in pairs.items():
        path = _path(path_id)
        if not isinstance(path, rules_paths.BranchPath) or path.fault:
            raise ValueError(
                f"{case_id}: branch_passes names {path_id!r}, which is not an inline branch"
            )
        if path_id not in reaches:
            raise ValueError(f"{case_id}: branch_passes names {path_id!r}, not under reaches")
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(isinstance(i, int) and not isinstance(i, bool) for i in pair)
            or pair[0] == pair[1]
            or not all(0 <= i < count for i in pair)
        ):
            raise ValueError(
                f"{case_id}: branch_passes[{path_id!r}] = {pair!r} is not two of its {count} passes"
            )


def _validate_spy_keys(case_id: str, case: Mapping[str, Any]) -> None:
    if "returned_spy_keys" not in case:
        return
    keys = case["returned_spy_keys"]
    if case.get("kind") != CALLER_KIND:
        raise ValueError(f"{case_id}: returned_spy_keys is for {CALLER_KIND} cases only")
    if (
        not isinstance(keys, list)
        or not keys
        or not all(isinstance(key, str) and key for key in keys)
        or len(set(keys)) != len(keys)
    ):
        raise ValueError(
            f"{case_id}: returned_spy_keys must be a non-empty list of distinct names: {keys!r}"
        )
    returned = case.get("expect", {}).get("returned")
    if not isinstance(returned, Mapping):
        raise ValueError(f"{case_id}: returned_spy_keys needs a mapping expect.returned")
    absent = [key for key in keys if key not in returned]
    if absent:
        raise ValueError(f"{case_id}: returned_spy_keys names keys not returned: {absent}")
    if set(keys) == set(returned):
        raise ValueError(f"{case_id}: returned_spy_keys may not name every returned key")


def validate_case_paths(case_id: str, case: Mapping[str, Any]) -> None:
    """Refuse a malformed entry, coverage, twin, fault, branch-pass or spy-key declaration."""
    _validate_entry(case_id, case)
    _validate_coverage(case_id, case)
    _validate_spy_keys(case_id, case)


def _leaves(value: Any, prefix: str = "") -> Iterable[str]:
    if isinstance(value, Mapping) and value:
        for key, item in value.items():
            yield from _leaves(item, f"{prefix}{key}.")
    elif isinstance(value, list) and value:
        for index, item in enumerate(value):
            yield from _leaves(item, f"{prefix}{index}.")
    else:
        yield prefix.rstrip(".")


def _input_paths(case: Mapping[str, Any]) -> set[str]:
    inputs = {key: value for key, value in case.items() if key not in _BOOKKEEPING_KEYS}
    inputs.setdefault("faults", [])
    return set(_leaves(inputs))


def _present(path: str, leaves: set[str]) -> bool:
    return any(leaf == path or leaf.startswith(path + ".") for leaf in leaves)


def validate_corpus_paths(cases: list[dict[str, Any]], manifest: Mapping[str, Any]) -> None:
    """Refuse manifest fault kinds that differ from the fault registry, a fault kind the
    manifest omits, a twin naming no case, a differs_at path in neither twin and an inline
    branch in no twin pair and no ``branch_passes`` entry."""
    by_id = {str(case["id"]): case for case in cases}
    kinds = manifest.get("fault_kinds")
    if kinds is not None and sorted(kinds) != sorted(rules_faults.FAULT_KINDS):
        raise ValueError(
            f"manifest.json: fault_kinds {sorted(kinds)} differ from the fault registry's"
            f" kinds {sorted(rules_faults.FAULT_KINDS)}"
        )
    named_by_twin: set[str] = set()
    for case in cases:
        case_id = str(case["id"])
        if kinds is not None:
            for fault in case.get("faults", []):
                if fault.get("kind") not in kinds:
                    raise ValueError(f"{case_id}: fault kind {fault.get('kind')!r} not in manifest")
        if "twin_of" not in case:
            continue
        base = by_id.get(str(case["twin_of"]))
        if base is None:
            raise ValueError(f"{case_id}: twin_of names no loaded case: {case['twin_of']!r}")
        named_by_twin.add(str(case["twin_of"]))
        leaves = _input_paths(case) | _input_paths(base)
        absent = [path for path in case["differs_at"] if not _present(path, leaves)]
        if absent:
            raise ValueError(f"{case_id}: differs_at names paths in neither twin: {absent}")
    for case in cases:
        case_id = str(case["id"])
        for path_id in case.get("reaches", []):
            path = _path(path_id)
            if not isinstance(path, rules_paths.BranchPath) or path.fault:
                continue
            paired = (
                "twin_of" in case
                or path_id in case.get("branch_passes", {})
                or case_id in named_by_twin
            )
            if not paired:
                raise ValueError(
                    f"{case_id}: branch {path_id} has no twin pair and no branch_passes entry"
                )
