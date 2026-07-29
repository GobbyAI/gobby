"""Canonical carry-forward ledger for non-blocking review observations."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import cast

from gobby.plans.review_evidence_models import (
    ReviewEvidenceError,
    canonical_json_object,
)
from gobby.plans.review_findings import (
    CHECK_KEY_RE,
    FINDING_CATEGORIES,
    FINDING_SEVERITIES,
    finding_ledger_details,
)
from gobby.plans.review_sweeps import (
    validate_candidate_dispositions as validate_sweep_candidate_dispositions,
)
from gobby.utils.hashing import is_sha256

_KINDS = frozenset({"finding", "dismissed"})
_COMMON_FIELDS = frozenset(
    {
        "ledger_entry_id",
        "kind",
        "check_key",
        "aliases",
        "first_seen_round",
        "rounds_carried",
        "source_section_ids",
        "section_hashes_at_entry",
        "stale",
    }
)
_FINDING_FIELDS = frozenset(
    {
        "category",
        "severity",
        "location",
        "description",
        "minimal_repair",
        "repair_scope",
        "new_deliverable_justification",
        "prevention",
        "principle",
        "root_cause",
    }
)
_DISMISSED_FIELDS = frozenset({"source_hash", "rationale"})
DISMISSED_LEDGER_CONTEXT_KEY = "dismissed_ledger_entries"
_FINDING_REQUIRED = (
    "category",
    "severity",
    "location",
    "description",
    "minimal_repair",
    "repair_scope",
    "prevention",
)
_DISPOSITION_FIELDS = frozenset(
    {
        "candidate_id",
        "check_key",
        "source_section_ids",
        "source_hash",
        "disposition",
        "rationale",
        "finding_id",
    }
)

LedgerKey = tuple[str, str, tuple[tuple[str, str], ...]]


def merge_quality_ledger(
    *,
    prior_ledger: Sequence[Mapping[str, object]],
    round_number: int,
    current_section_hashes: Mapping[str, str],
    round_result: Mapping[str, object],
    prior_round_context: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    """Merge one reviewed round into the durable quality ledger."""
    if round_number < 1:
        raise _invalid("round_number must be a positive integer")
    section_hashes = _validate_section_hashes(current_section_hashes)
    ledger = validate_quality_ledger(prior_ledger)
    dispositions = validate_candidate_dispositions(round_result)
    touched: set[str] = set()

    for entry in ledger:
        if entry["stale"] is True:
            continue
        stored_hashes = cast(dict[str, str], entry["section_hashes_at_entry"])
        if any(
            section_hashes.get(section_id) != value for section_id, value in stored_hashes.items()
        ):
            entry["stale"] = True

    active = {_entry_key(entry): entry for entry in ledger if entry["stale"] is False}
    _merge_carry_resolutions(
        ledger=ledger,
        active=active,
        touched=touched,
        round_number=round_number,
        section_hashes=section_hashes,
        prior_round_context=prior_round_context,
    )

    findings = round_result.get("findings")
    if not isinstance(findings, list) or any(not isinstance(item, Mapping) for item in findings):
        raise _invalid("round_result.findings must be an array of objects")
    for index, raw in enumerate(findings):
        finding = canonical_json_object(cast(Mapping[str, object], raw))
        if finding.get("severity") == "blocking":
            continue
        _merge_finding(
            ledger=ledger,
            active=active,
            touched=touched,
            round_number=round_number,
            section_hashes=section_hashes,
            finding=finding,
            owner=f"round_result.findings[{index}]",
        )

    for disposition in dispositions:
        if disposition["disposition"] != "dismissed":
            continue
        _merge_dismissal(
            ledger=ledger,
            active=active,
            touched=touched,
            round_number=round_number,
            section_hashes=section_hashes,
            disposition=disposition,
        )
    return validate_quality_ledger(ledger)


def validate_quality_ledger(
    raw_ledger: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Validate and canonicalize persisted ledger entries."""
    payload = canonical_json_object({"quality_ledger": list(raw_ledger)})
    raw_entries = payload["quality_ledger"]
    if not isinstance(raw_entries, list):
        raise _invalid("quality_ledger must be an array")
    entries: list[dict[str, object]] = []
    ids: set[str] = set()
    keys: set[LedgerKey] = set()
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            raise _invalid(f"quality_ledger[{index}] must be an object")
        entry = _validate_entry(raw, index=index)
        entry_id = cast(str, entry["ledger_entry_id"])
        if entry_id in ids:
            raise _invalid(f"duplicate ledger_entry_id: {entry_id}")
        key = _entry_key(entry)
        if key in keys:
            raise _invalid(f"duplicate quality ledger key: {entry['check_key']}")
        ids.add(entry_id)
        keys.add(key)
        entries.append(entry)
    return entries


def inject_dismissed_ledger_context(
    *,
    prior_round_context: Mapping[str, object],
    prior_ledger: Sequence[Mapping[str, object]],
    current_section_hashes: Mapping[str, str],
) -> dict[str, object]:
    """Inject active dismissals with a server-derived section-hash reopen marker."""
    context = canonical_json_object(prior_round_context)
    section_hashes = _validate_section_hashes(current_section_hashes)
    dismissed: list[dict[str, object]] = []
    for entry in validate_quality_ledger(prior_ledger):
        if entry["kind"] != "dismissed" or entry["stale"] is True:
            continue
        stored_hashes = cast(dict[str, str], entry["section_hashes_at_entry"])
        contextual_entry = dict(entry)
        contextual_entry["reopenable"] = any(
            section_hashes.get(section_id) != section_hash
            for section_id, section_hash in stored_hashes.items()
        )
        dismissed.append(contextual_entry)
    context[DISMISSED_LEDGER_CONTEXT_KEY] = dismissed
    return context


def dismissed_ledger_entries_from_context(
    prior_round_context: Mapping[str, object] | None,
) -> list[dict[str, object]]:
    """Validate and return the server-owned dismissed-ledger context projection."""
    if prior_round_context is None:
        return []
    context = canonical_json_object(prior_round_context)
    if DISMISSED_LEDGER_CONTEXT_KEY not in context:
        raise _invalid(f"prior_round_context.{DISMISSED_LEDGER_CONTEXT_KEY} is required")
    raw_entries = context[DISMISSED_LEDGER_CONTEXT_KEY]
    if not isinstance(raw_entries, list):
        raise _invalid(f"prior_round_context.{DISMISSED_LEDGER_CONTEXT_KEY} must be an array")
    ledger_entries: list[dict[str, object]] = []
    reopenable_flags: list[bool] = []
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, Mapping):
            raise _invalid(f"{DISMISSED_LEDGER_CONTEXT_KEY}[{index}] must be an object")
        entry = canonical_json_object(raw)
        reopenable = entry.pop("reopenable", None)
        if not isinstance(reopenable, bool):
            raise _invalid(f"{DISMISSED_LEDGER_CONTEXT_KEY}[{index}].reopenable must be boolean")
        ledger_entries.append(entry)
        reopenable_flags.append(reopenable)
    validated = validate_quality_ledger(ledger_entries)
    result: list[dict[str, object]] = []
    for index, entry in enumerate(validated):
        if entry["kind"] != "dismissed" or entry["stale"] is not False:
            raise _invalid(
                f"{DISMISSED_LEDGER_CONTEXT_KEY}[{index}] must be an active dismissed entry"
            )
        contextual_entry = dict(entry)
        contextual_entry["reopenable"] = reopenable_flags[index]
        result.append(contextual_entry)
    return result


def validate_candidate_dispositions(
    round_result: Mapping[str, object],
) -> list[dict[str, object]]:
    """Validate durable candidate records against coverage disposition counts."""
    coverage = round_result.get("coverage_attestation")
    if not isinstance(coverage, Mapping):
        raise _invalid("round_result.coverage_attestation must be an object")
    counts = coverage.get("disposition_counts")
    if not isinstance(counts, Mapping):
        raise _invalid("coverage_attestation.disposition_counts must be an object")
    bundle = coverage.get("record_bundle")
    if not isinstance(bundle, Mapping):
        raise _invalid("coverage_attestation.record_bundle must be an object")
    records = validate_sweep_candidate_dispositions(
        bundle.get("candidate_dispositions"),
        candidates=None,
    )
    finding_ids = {
        finding_id
        for finding in cast(Sequence[object], round_result.get("findings", []))
        if isinstance(finding, Mapping) and isinstance(finding_id := finding.get("finding_id"), str)
    }
    for index, record in enumerate(records):
        if record["disposition"] != "emitted_finding":
            continue
        finding_id = cast(str, record["finding_id"])
        if finding_id not in finding_ids:
            raise _invalid(
                f"candidate_dispositions[{index}].finding_id is absent from round_result.findings"
            )

    computed = {
        "total": len(records),
        "emitted_findings": sum(record["disposition"] == "emitted_finding" for record in records),
        "dismissed": sum(record["disposition"] == "dismissed" for record in records),
    }
    if dict(counts) != computed:
        raise _invalid(
            "coverage_attestation.disposition_counts disagree with candidate_dispositions"
        )
    return records


def _merge_carry_resolutions(
    *,
    ledger: list[dict[str, object]],
    active: dict[LedgerKey, dict[str, object]],
    touched: set[str],
    round_number: int,
    section_hashes: Mapping[str, str],
    prior_round_context: Mapping[str, object] | None,
) -> None:
    if prior_round_context is None:
        return
    resolutions = prior_round_context.get("prior_finding_resolutions", [])
    if not isinstance(resolutions, list):
        raise _invalid("prior_round_context.prior_finding_resolutions must be an array")
    for index, raw in enumerate(resolutions):
        if not isinstance(raw, Mapping):
            raise _invalid(f"prior_finding_resolutions[{index}] must be an object")
        if raw.get("decision") != "carry":
            continue
        finding_id = _required_string(
            raw,
            "prior_finding_id",
            f"prior_finding_resolutions[{index}]",
        )
        source = next(
            (
                entry
                for entry in reversed(ledger)
                if entry["kind"] == "finding" and finding_id in cast(list[str], entry["aliases"])
            ),
            None,
        )
        if source is None:
            raise _invalid(f"carry resolution has no quality ledger entry: {finding_id}")
        source_ids = cast(list[str], source["source_section_ids"])
        if any(section_id not in section_hashes for section_id in source_ids):
            source["stale"] = True
            continue
        key = _canonical_key(
            kind="finding",
            check_key=cast(str, source["check_key"]),
            source_section_ids=source_ids,
            section_hashes=section_hashes,
        )
        target = active.get(key)
        if target is None:
            target = _freshened_entry(
                source,
                round_number=round_number,
                section_hashes=section_hashes,
            )
            ledger.append(target)
            active[key] = target
            touched.add(cast(str, target["ledger_entry_id"]))
        else:
            _touch(target, touched=touched)


def _merge_finding(
    *,
    ledger: list[dict[str, object]],
    active: dict[LedgerKey, dict[str, object]],
    touched: set[str],
    round_number: int,
    section_hashes: Mapping[str, str],
    finding: Mapping[str, object],
    owner: str,
) -> None:
    check_key = _validate_check_key(finding.get("check_key"), owner=owner)
    category = finding.get("category")
    if category not in FINDING_CATEGORIES:
        raise _invalid(f"{owner}.category is not a supported adversary category")
    severity = finding.get("severity")
    if severity not in FINDING_SEVERITIES - {"blocking"}:
        raise _invalid(f"{owner}.severity is not a non-blocking finding severity")
    finding_id = _required_string(finding, "finding_id", owner)
    source_ids = _finding_section_ids(finding, owner=owner)
    if any(section_id not in section_hashes for section_id in source_ids):
        return
    key = _canonical_key(
        kind="finding",
        check_key=check_key,
        source_section_ids=source_ids,
        section_hashes=section_hashes,
    )
    entry = active.get(key)
    details = _finding_details(finding, owner=owner)
    if entry is None:
        entry = _new_entry(
            kind="finding",
            check_key=check_key,
            alias=finding_id,
            round_number=round_number,
            source_section_ids=source_ids,
            section_hashes=section_hashes,
            details=details,
        )
        ledger.append(entry)
        active[key] = entry
        touched.add(cast(str, entry["ledger_entry_id"]))
        return
    entry.update(details)
    _add_alias(entry, finding_id)
    _touch(entry, touched=touched)


def _merge_dismissal(
    *,
    ledger: list[dict[str, object]],
    active: dict[LedgerKey, dict[str, object]],
    touched: set[str],
    round_number: int,
    section_hashes: Mapping[str, str],
    disposition: Mapping[str, object],
) -> None:
    source_ids = cast(list[str], disposition["source_section_ids"])
    check_key = cast(str, disposition["check_key"])
    key = _canonical_key(
        kind="dismissed",
        check_key=check_key,
        source_section_ids=source_ids,
        section_hashes=section_hashes,
    )
    candidate_id = cast(str, disposition["candidate_id"])
    details = {
        "source_hash": disposition["source_hash"],
        "rationale": disposition["rationale"],
    }
    entry = active.get(key)
    if entry is None:
        entry = _new_entry(
            kind="dismissed",
            check_key=check_key,
            alias=candidate_id,
            round_number=round_number,
            source_section_ids=source_ids,
            section_hashes=section_hashes,
            details=details,
        )
        ledger.append(entry)
        active[key] = entry
        touched.add(cast(str, entry["ledger_entry_id"]))
        return
    entry.update(details)
    _add_alias(entry, candidate_id)
    _touch(entry, touched=touched)


def _new_entry(
    *,
    kind: str,
    check_key: str,
    alias: str,
    round_number: int,
    source_section_ids: Sequence[str],
    section_hashes: Mapping[str, str],
    details: Mapping[str, object],
    first_seen_round: int | None = None,
    rounds_carried: int = 1,
) -> dict[str, object]:
    source_ids = sorted(set(source_section_ids))
    hashes_at_entry = {section_id: section_hashes[section_id] for section_id in source_ids}
    first_seen = round_number if first_seen_round is None else first_seen_round
    key = _canonical_key(
        kind=kind,
        check_key=check_key,
        source_section_ids=source_ids,
        section_hashes=hashes_at_entry,
    )
    entry: dict[str, object] = {
        "ledger_entry_id": _ledger_entry_id(key, first_seen_round=first_seen),
        "kind": kind,
        "check_key": check_key,
        "aliases": [alias],
        "first_seen_round": first_seen,
        "rounds_carried": rounds_carried,
        "source_section_ids": source_ids,
        "section_hashes_at_entry": hashes_at_entry,
        "stale": False,
    }
    entry.update(details)
    return entry


def _freshened_entry(
    source: Mapping[str, object],
    *,
    round_number: int,
    section_hashes: Mapping[str, str],
) -> dict[str, object]:
    source_ids = cast(list[str], source["source_section_ids"])
    details = {
        key: value for key, value in source.items() if key in _FINDING_FIELDS | _DISMISSED_FIELDS
    }
    entry = _new_entry(
        kind=cast(str, source["kind"]),
        check_key=cast(str, source["check_key"]),
        alias=cast(list[str], source["aliases"])[0],
        round_number=round_number,
        source_section_ids=source_ids,
        section_hashes=section_hashes,
        details=details,
        first_seen_round=cast(int, source["first_seen_round"]),
        rounds_carried=cast(int, source["rounds_carried"]) + 1,
    )
    entry["aliases"] = list(cast(list[str], source["aliases"]))
    return entry


def _finding_details(
    finding: Mapping[str, object],
    *,
    owner: str,
) -> dict[str, object]:
    return finding_ledger_details(finding, owner=owner)


def _finding_section_ids(
    finding: Mapping[str, object],
    *,
    owner: str,
) -> list[str]:
    section_ids = {_required_string(finding, "section_id", owner)}
    for field in ("participating_section_ids", "causal_section_ids"):
        if field not in finding:
            continue
        raw = finding[field]
        if not isinstance(raw, list) or any(
            not isinstance(value, str) or not value for value in raw
        ):
            raise _invalid(f"{owner}.{field} must be an array of non-empty strings")
        section_ids.update(cast(list[str], raw))
    return sorted(section_ids)


def _validate_entry(raw: dict[str, object], *, index: int) -> dict[str, object]:
    owner = f"quality_ledger[{index}]"
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in _KINDS:
        raise _invalid(f"{owner}.kind must be finding or dismissed")
    allowed = _COMMON_FIELDS | (_FINDING_FIELDS if kind == "finding" else _DISMISSED_FIELDS)
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise _invalid(f"{owner} has unknown fields: {', '.join(unknown)}")
    required = _COMMON_FIELDS | (
        frozenset(_FINDING_REQUIRED) if kind == "finding" else _DISMISSED_FIELDS
    )
    missing = sorted(required - set(raw))
    if missing:
        raise _invalid(f"{owner} is missing fields: {', '.join(missing)}")
    entry_id = _required_string(raw, "ledger_entry_id", owner)
    if not re.fullmatch(r"ledger-[0-9a-f]{64}", entry_id):
        raise _invalid(f"{owner}.ledger_entry_id is invalid")
    _validate_check_key(raw.get("check_key"), owner=owner)
    aliases = _string_set(raw.get("aliases"), owner=f"{owner}.aliases")
    raw["aliases"] = aliases
    first_seen = raw.get("first_seen_round")
    carried = raw.get("rounds_carried")
    if not _positive_int(first_seen):
        raise _invalid(f"{owner}.first_seen_round must be a positive integer")
    if not _positive_int(carried):
        raise _invalid(f"{owner}.rounds_carried must be a positive integer")
    if not isinstance(raw.get("stale"), bool):
        raise _invalid(f"{owner}.stale must be a boolean")
    source_ids = _string_set(
        raw.get("source_section_ids"),
        owner=f"{owner}.source_section_ids",
    )
    hashes = raw.get("section_hashes_at_entry")
    if not isinstance(hashes, dict) or set(hashes) != set(source_ids):
        raise _invalid(f"{owner}.section_hashes_at_entry must map every source section")
    for section_id, value in hashes.items():
        _validate_hash(value, owner=f"{owner}.section_hashes_at_entry[{section_id}]")
    raw["source_section_ids"] = source_ids
    raw["section_hashes_at_entry"] = {section_id: hashes[section_id] for section_id in source_ids}
    if kind == "finding":
        if not isinstance(raw.get("category"), str) or raw["category"] not in FINDING_CATEGORIES:
            raise _invalid(f"{owner}.category is not a supported adversary category")
        if not isinstance(raw.get("severity"), str) or raw["severity"] not in FINDING_SEVERITIES - {
            "blocking"
        }:
            raise _invalid(f"{owner}.severity is not a non-blocking finding severity")
        _finding_details(raw, owner=owner)
    else:
        _validate_hash(raw.get("source_hash"), owner=f"{owner}.source_hash")
        _required_string(raw, "rationale", owner)
    return raw


def _validate_section_hashes(raw: Mapping[str, str]) -> dict[str, str]:
    hashes = dict(raw)
    for section_id, value in hashes.items():
        if not isinstance(section_id, str) or not section_id:
            raise _invalid("current_section_hashes requires non-empty section IDs")
        _validate_hash(value, owner=f"current_section_hashes[{section_id}]")
    return hashes


def _canonical_key(
    *,
    kind: str,
    check_key: str,
    source_section_ids: Sequence[str],
    section_hashes: Mapping[str, str],
) -> LedgerKey:
    source_ids = sorted(set(source_section_ids))
    if not source_ids:
        raise _invalid("quality ledger entries require source sections")
    try:
        pairs = tuple((section_id, section_hashes[section_id]) for section_id in source_ids)
    except KeyError as exc:
        raise _invalid(f"quality ledger references unknown section: {exc.args[0]}") from exc
    return kind, check_key, pairs


def _entry_key(entry: Mapping[str, object]) -> LedgerKey:
    return _canonical_key(
        kind=cast(str, entry["kind"]),
        check_key=cast(str, entry["check_key"]),
        source_section_ids=cast(list[str], entry["source_section_ids"]),
        section_hashes=cast(dict[str, str], entry["section_hashes_at_entry"]),
    )


def _ledger_entry_id(key: LedgerKey, *, first_seen_round: int) -> str:
    encoded = json.dumps(
        [key[0], key[1], key[2], first_seen_round],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"ledger-{hashlib.sha256(encoded).hexdigest()}"


def _touch(entry: dict[str, object], *, touched: set[str]) -> None:
    entry_id = cast(str, entry["ledger_entry_id"])
    if entry_id in touched:
        return
    entry["rounds_carried"] = cast(int, entry["rounds_carried"]) + 1
    touched.add(entry_id)


def _add_alias(entry: dict[str, object], alias: str) -> None:
    aliases = cast(list[str], entry["aliases"])
    entry["aliases"] = sorted(set(aliases) | {alias})


def _validate_check_key(raw: object, *, owner: str) -> str:
    if not isinstance(raw, str) or CHECK_KEY_RE.fullmatch(raw) is None:
        raise _invalid(f"{owner}.check_key is invalid")
    return raw


def _string_set(raw: object, *, owner: str) -> list[str]:
    if (
        not isinstance(raw, list)
        or not raw
        or any(not isinstance(value, str) or not value for value in raw)
    ):
        raise _invalid(f"{owner} must be a non-empty array of strings")
    values = cast(list[str], raw)
    if len(values) != len(set(values)):
        raise _invalid(f"{owner} contains duplicates")
    return sorted(values)


def _required_string(payload: Mapping[str, object], field: str, owner: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise _invalid(f"{owner}.{field} must be a non-empty string")
    return value


def _validate_hash(raw: object, *, owner: str) -> str:
    if not is_sha256(raw):
        raise _invalid(f"{owner} must be a lowercase SHA-256")
    return raw


def _positive_int(raw: object) -> bool:
    return isinstance(raw, int) and not isinstance(raw, bool) and raw > 0


def _invalid(message: str) -> ReviewEvidenceError:
    return ReviewEvidenceError("invalid_quality_ledger", message)
