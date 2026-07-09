"""Harvest rendered <project-memory> injections from transcripts (task #17193).

Sources:
1. ~/.gobby/session_transcripts/*.jsonl.gz  (normalized store; external_id = stem)
2. ~/.claude/projects/*/*.jsonl             (raw Claude Code; external_id = stem)
3. ~/.codex/sessions/**/rollout-*.jsonl     (raw Codex; external_id = trailing uuid)

Emits one row per (transcript, injection-block, memory entry) to injections.jsonl.
"""

import gzip
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import psycopg
import yaml

SCRATCH = Path(__file__).parent
BLOCK_MARK = "<project-memory>"
# One rendered entry: "- <content> (memory_id: <uuid>[, score: <f>][, via: <s>])"
ENTRY_RE = re.compile(
    r"\(memory_id: ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"(?:, score: ([0-9.]+))?(?:, via: ([a-zA-Z_.-]+))?\)"
)
BLOCK_RE = re.compile(r"<project-memory>\n(.*?)</project-memory>", re.DOTALL)


def parse_block(text: str) -> list[dict[str, Any]]:
    """Parse rendered block bodies out of a text payload."""
    entries: list[dict[str, Any]] = []
    for m in BLOCK_RE.finditer(text):
        body = m.group(1)
        # Entries start at line-initial "- "; content may contain newlines.
        parts = re.split(r"\n(?=- )", body)
        for pos, part in enumerate(parts):
            if not part.startswith("- "):
                continue
            em = ENTRY_RE.search(part)
            if not em:
                continue
            content = part[2 : em.start()].strip()
            entries.append(
                {
                    "memory_id": em.group(1),
                    "render_score": float(em.group(2)) if em.group(2) else None,
                    "render_via": em.group(3),
                    "content": content,
                    "block_position": pos,
                    "block_size": sum(1 for p in parts if p.startswith("- ")),
                }
            )
    return entries


def _text_of_claude_message(msg: dict[str, Any]) -> str:
    mc = msg.get("content")
    if isinstance(mc, str):
        return mc
    out: list[str] = []
    if isinstance(mc, list):
        for b in mc:
            if isinstance(b, dict) and b.get("type") == "text":
                out.append(str(b.get("text", "")))
    return "\n".join(out)


def extract_claude(lines: list[str]) -> list[dict[str, Any]]:
    """Extract injection turns from claude-raw / gobby-store format."""
    parsed: list[dict[str, Any] | None] = []
    for ln in lines:
        try:
            o = json.loads(ln)
            parsed.append(o if isinstance(o, dict) else None)
        except json.JSONDecodeError:
            parsed.append(None)

    injections: list[dict[str, Any]] = []
    for i, obj in enumerate(parsed):
        if not obj:
            continue
        texts: list[str] = []
        if obj.get("type") == "attachment":
            att = obj.get("attachment") or {}
            if att.get("type") == "hook_additional_context":
                for item in att.get("content") or []:
                    if isinstance(item, str) and BLOCK_MARK in item:
                        texts.append(item)
        elif obj.get("type") == "user":
            msg = obj.get("message") or {}
            t = _text_of_claude_message(msg)
            if BLOCK_MARK in t:
                texts.append(t)
        if not texts:
            continue
        entries: list[dict[str, Any]] = []
        for t in texts:
            entries.extend(parse_block(t))
        if not entries:
            continue

        # user prompt: nearest preceding type==user with plain-string content
        prompt = ""
        for j in range(i, -1, -1):
            o = parsed[j]
            if not o or o.get("type") != "user":
                continue
            msg = o.get("message") or {}
            mc = msg.get("content")
            if isinstance(mc, str) and mc.strip():
                prompt = mc
                break
            if isinstance(mc, list):
                blocks = [
                    b.get("text", "") for b in mc if isinstance(b, dict) and b.get("type") == "text"
                ]
                joined = "\n".join(blocks).strip()
                if joined and "tool_result" not in {
                    b.get("type") for b in mc if isinstance(b, dict)
                }:
                    prompt = joined
                    break

        # assistant response: following assistant text blocks until the next
        # real user prompt (string content user line)
        resp_parts: list[str] = []
        for j in range(i + 1, len(parsed)):
            o = parsed[j]
            if not o:
                continue
            if o.get("type") == "user":
                msg = o.get("message") or {}
                if isinstance(msg.get("content"), str):
                    break
            if o.get("type") == "assistant":
                t = _text_of_claude_message(o.get("message") or {})
                if t.strip():
                    resp_parts.append(t)
            if sum(len(x) for x in resp_parts) > 20000:
                break
        injections.append(
            {
                "line_index": i,
                "timestamp": obj.get("timestamp"),
                "entries": entries,
                "user_prompt": prompt,
                "assistant_response": "\n\n".join(resp_parts),
            }
        )
    return injections


def extract_codex(lines: list[str]) -> list[dict[str, Any]]:
    """Extract injection turns from codex rollout format."""
    parsed: list[dict[str, Any] | None] = []
    for ln in lines:
        try:
            o = json.loads(ln)
            parsed.append(o if isinstance(o, dict) else None)
        except json.JSONDecodeError:
            parsed.append(None)

    injections: list[dict[str, Any]] = []
    for i, obj in enumerate(parsed):
        if not obj or obj.get("type") != "response_item":
            continue
        pl = obj.get("payload") or {}
        if pl.get("type") != "message" or pl.get("role") not in ("developer", "user"):
            continue
        texts = [
            str(c.get("text", ""))
            for c in pl.get("content") or []
            if isinstance(c, dict) and BLOCK_MARK in str(c.get("text", ""))
        ]
        if not texts:
            continue
        entries: list[dict[str, Any]] = []
        for t in texts:
            entries.extend(parse_block(t))
        if not entries:
            continue

        prompt = ""
        for j in range(i - 1, -1, -1):
            o = parsed[j]
            if not o or o.get("type") != "response_item":
                continue
            p2 = o.get("payload") or {}
            if p2.get("type") == "message" and p2.get("role") == "user":
                cand = "\n".join(
                    str(c.get("text", "")) for c in p2.get("content") or [] if isinstance(c, dict)
                )
                if BLOCK_MARK not in cand and cand.strip():
                    prompt = cand
                    break

        resp_parts: list[str] = []
        for j in range(i + 1, len(parsed)):
            o = parsed[j]
            if not o:
                continue
            if o.get("type") != "response_item":
                continue
            p2 = o.get("payload") or {}
            if p2.get("type") == "message" and p2.get("role") == "user":
                break
            if p2.get("type") == "message" and p2.get("role") == "assistant":
                for c in p2.get("content") or []:
                    if isinstance(c, dict) and str(c.get("text", "")).strip():
                        resp_parts.append(str(c.get("text", "")))
            if sum(len(x) for x in resp_parts) > 20000:
                break
        injections.append(
            {
                "line_index": i,
                "timestamp": obj.get("timestamp"),
                "entries": entries,
                "user_prompt": prompt,
                "assistant_response": "\n\n".join(resp_parts),
            }
        )
    return injections


def main() -> None:
    store = Path.home() / ".gobby" / "session_transcripts"
    claude_root = Path.home() / ".claude" / "projects"
    codex_root = Path.home() / ".codex" / "sessions"

    files: dict[str, tuple[Path, str]] = {}  # external_id -> (path, fmt)
    for p in store.glob("*.jsonl.gz"):
        files[p.name.split(".jsonl")[0]] = (p, "store")
    for p in claude_root.glob("*/*.jsonl"):
        files.setdefault(p.stem, (p, "claude"))
    proc = subprocess.run(
        ["find", str(codex_root), "-name", "rollout-*.jsonl"],
        capture_output=True,
        text=True,
        check=True,
    )
    for fp in proc.stdout.splitlines():
        p = Path(fp)
        parts = p.stem.split("-")
        if len(parts) >= 5:
            files.setdefault("-".join(parts[-5:]), (p, "codex"))

    print(f"candidate transcript files (deduped by external_id): {len(files)}")

    rows: list[dict[str, Any]] = []
    scanned = 0
    with_blocks = 0
    for ext_id, (p, fmt) in files.items():
        try:
            if p.suffix == ".gz":
                raw = gzip.open(p, "rt", errors="replace").read()
            else:
                raw = p.read_text(errors="replace")
        except OSError:
            continue
        scanned += 1
        if BLOCK_MARK not in raw:
            continue
        lines = raw.splitlines()
        if fmt == "codex":
            injections = extract_codex(lines)
        else:
            injections = extract_claude(lines)
        if not injections:
            continue
        with_blocks += 1
        for inj in injections:
            for e in inj["entries"]:
                rows.append(
                    {
                        "external_id": ext_id,
                        "file": str(p),
                        "format": fmt,
                        "line_index": inj["line_index"],
                        "timestamp": inj["timestamp"],
                        "user_prompt": inj["user_prompt"],
                        "assistant_response": inj["assistant_response"],
                        **e,
                    }
                )

    print(f"scanned {scanned}; transcripts with parsed injections: {with_blocks}")
    print(f"injected-memory rows: {len(rows)}")

    # Map external_id -> session (platform id, source, project)
    boot = yaml.safe_load((Path.home() / ".gobby" / "bootstrap.yaml").read_text())
    ext_ids = sorted({r["external_id"] for r in rows})
    session_map: dict[str, dict[str, str]] = {}
    with psycopg.connect(boot["database_url"], connect_timeout=10) as conn:
        for batch_start in range(0, len(ext_ids), 500):
            batch = ext_ids[batch_start : batch_start + 500]
            for sid, ext, source, project_id in conn.execute(
                "SELECT id, external_id, source, project_id FROM sessions"
                " WHERE external_id = ANY(%s)",
                (batch,),
            ):
                session_map[str(ext)] = {
                    "session_id": str(sid),
                    "source": str(source),
                    "project_id": str(project_id),
                }

    mapped = 0
    for r in rows:
        info = session_map.get(r["external_id"])
        if info:
            r.update(info)
            mapped += 1
        else:
            r["session_id"] = None
            r["source"] = None
            r["project_id"] = None
    print(f"rows mapped to a platform session: {mapped}/{len(rows)}")

    out = SCRATCH / "injections.jsonl"
    with out.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
