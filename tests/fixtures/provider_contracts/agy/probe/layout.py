"""Record the brain/<id> transcript layout, chunk identity, and record census.

Usage: python3 probe/layout.py <CONVERSATION_ID> [needle ...]

Lists every file under ``~/.gemini/antigravity-cli/brain/<id>`` with its size, compares
each ``chunks/<name>/00000000.jsonl`` against its parent file, counts ``source/type``
pairs in ``transcript_full.jsonl``, and prints the records containing each needle.
Record 1.1.22 (interactive conversation).
"""

import collections
import filecmp
import json
import sys
from pathlib import Path


def main(conv: str, needles: list[str]) -> None:
    brain = Path.home() / ".gemini" / "antigravity-cli" / "brain" / conv
    logs = brain / ".system_generated" / "logs"
    for path in sorted(p for p in brain.rglob("*") if p.is_file()):
        print(f"{path.stat().st_size:8d} {path.relative_to(brain)}")
    for name in ("transcript", "transcript_full"):
        chunk = logs / "chunks" / name / "00000000.jsonl"
        same = filecmp.cmp(logs / f"{name}.jsonl", chunk, shallow=False)
        print(f"cmp {name}.jsonl chunks/{name}/00000000.jsonl: {'identical' if same else 'DIFFER'}")
    census: collections.Counter[tuple[str, str]] = collections.Counter()
    records: list[dict[str, object]] = []
    for line in (logs / "transcript_full.jsonl").read_text().splitlines():
        rec = json.loads(line)
        census[(rec.get("source"), rec.get("type"))] += 1
        records.append(rec)
    print("census:", {f"{s}/{t}": n for (s, t), n in census.items()})
    for needle in needles:
        for rec in records:
            if needle in json.dumps(rec):
                print(f"--- record containing {needle!r}:")
                print(json.dumps(rec)[:1200])


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
