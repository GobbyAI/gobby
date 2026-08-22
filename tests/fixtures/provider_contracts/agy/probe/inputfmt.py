"""Probe ``agy --input-format stream-json`` persistent-session semantics (record 1.1.18).

Usage: python3 probe/inputfmt.py <case> [CONVERSATION_ID]

Drives ``agy --input-format stream-json --output-format stream-json --sandbox=false
--dangerously-skip-permissions --add-dir $GATE0_WORKSPACE --print-timeout 2m --model
gpt-oss-120b-medium`` over a pipe, logging each stdin line (``>>``) and each stdout
record with a timestamp, then prints the report, stdout, and stderr sections.

Cases:
  eof      one turn, then close stdin: EOF behavior and exit code
  conv     ``--conversation CONVERSATION_ID``: two turns on a resumed conversation
  idle     ``--print-timeout 20s`` with a 25 s idle gap between two turns
  cancel   SIGINT while a ``run: sleep 30`` tool step is ACTIVE, then another turn
  shapes   ``-p`` appended with no value, raw text line on stdin
  shapes2  raw text line on stdin
  shapes3  ``{"prompt": ...}`` without an ``event`` field
  shapes4  bogus event, then text content, then content blocks, then an image block
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

MODEL = "gpt-oss-120b-medium"
STDIN_SHAPES: dict[str, list[object]] = {
    "shapes": ["reply with exactly: alpha"],
    "shapes2": ["reply with exactly: alpha"],
    "shapes3": [{"prompt": "reply with exactly: beta"}],
    "shapes4": [
        {"event": "bogus_event"},
        {"event": "user", "message": {"content": "reply with exactly: beta"}},
        {
            "event": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "reply with exactly: gamma"}],
            },
        },
        {"event": "user", "message": {"content": [{"type": "image", "source": "x"}]}},
    ],
}


def user(text: str) -> dict[str, object]:
    return {"event": "user", "message": {"content": text}}


class Probe:
    def __init__(self, case: str, conv: str | None) -> None:
        self.case = case
        workspace = Path(
            os.environ.get("GATE0_WORKSPACE") or tempfile.mkdtemp(prefix="agy-gate0-ws.")
        )
        workspace.mkdir(parents=True, exist_ok=True)
        runs = Path(os.environ.get("GATE0_RUNS") or tempfile.mkdtemp(prefix="agy-gate0-runs."))
        self.out = runs / case
        self.out.mkdir(parents=True, exist_ok=True)
        self.report = (self.out / "report").open("w")
        self.lines: list[str] = []
        self.events: list[dict[str, Any]] = []  # raw stream-json records
        cmd = [
            "agy", "--input-format", "stream-json", "--output-format", "stream-json",
            "--sandbox=false", "--dangerously-skip-permissions", "--add-dir", str(workspace),
            "--print-timeout", "20s" if case == "idle" else "2m", "--model", MODEL,
        ]  # fmt: skip
        if conv:
            cmd += ["--conversation", conv]
        if case == "shapes":
            cmd.append("-p")
        self.say("cmd:", " ".join(cmd))
        self.t0 = time.time()
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=(self.out / "stderr").open("w"),
            text=True,
            cwd=workspace,
        )
        threading.Thread(target=self.reader, daemon=True).start()

    def say(self, *parts: object) -> None:
        print(*parts, file=self.report, flush=True)

    def stamp(self) -> str:
        return f"[{time.time() - self.t0:6.1f}]"

    def reader(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.append(line)
            try:
                record = json.loads(line)
            except ValueError:
                self.say(f"{self.stamp()} RAW {line[:200]!r}")
                continue
            self.events.append(record)
            step = record.get("step_update") or {}
            quiet = {"agent_response", "checkpoint", "user_input"}
            if record["event"] in ("init", "result") or step.get("step_type") not in quiet:
                body = record.get("result") or record.get("init") or step
                brief = {k: v for k, v in body.items() if k not in ("tools", "response")}
                self.say(f"{self.stamp()} {record['event']} {json.dumps(brief)[:260]}")

    def send(self, obj: object) -> None:
        assert self.proc.stdin is not None
        line = obj if isinstance(obj, str) else json.dumps(obj)
        self.say(f"{self.stamp()} >> {line[:200]}")
        try:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
        except BrokenPipeError:
            self.say(f"{self.stamp()} stdin closed by agy")

    def results(self) -> int:
        return sum(1 for e in self.events if e["event"] == "result")

    def wait_result(self, n: int, timeout: float = 150) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            if self.results() >= n:
                return True
            if self.proc.poll() is not None:
                return False
            time.sleep(0.2)
        return False

    def tool_active(self) -> bool:
        steps = (e.get("step_update") or {} for e in self.events)
        return any(s.get("state") == "ACTIVE" and s.get("step_type") == "tool" for s in steps)

    def run(self) -> None:
        case = self.case
        if case in STDIN_SHAPES:
            for shape in STDIN_SHAPES[case]:
                self.send(shape)
                self.wait_result(self.results() + 1, 60)
                if self.proc.poll() is not None:
                    self.say(f"process exited rc={self.proc.returncode}")
                    break
        elif case == "conv":
            self.send(user("reply with exactly: one"))
            self.wait_result(1)
            self.send(user("what did you reply last time? one word"))
            self.wait_result(2)
        elif case == "cancel":
            self.send(user("run: sleep 30; echo slept"))
            start = time.time()
            while time.time() - start < 60 and not self.tool_active():
                time.sleep(0.2)
            self.say(f"{self.stamp()} sending SIGINT to agy pid {self.proc.pid}")
            self.proc.send_signal(signal.SIGINT)
            time.sleep(3)
            alive = self.proc.poll() is None
            self.say(f"{self.stamp()} alive after SIGINT: {alive} rc={self.proc.poll()}")
            if alive:
                self.send(user("reply with exactly: after-cancel"))
                self.wait_result(2)
        elif case == "idle":
            self.send(user("reply with exactly: t1"))
            self.wait_result(1)
            self.say("idle 25s")
            time.sleep(25)
            self.say("alive after idle:", self.proc.poll() is None)
            self.send(user("reply with exactly: t2"))
            self.wait_result(2)
        elif case == "eof":
            self.send(user("reply with exactly: before-eof"))
            self.wait_result(1)
        else:
            raise SystemExit(f"unknown case {case!r}; see the module docstring")
        self.finish()

    def finish(self) -> None:
        assert self.proc.stdin is not None
        if self.proc.poll() is None:
            self.say(f"{self.stamp()} closing stdin (EOF)")
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self.say("still alive 60s after EOF; killing")
                self.proc.kill()
                self.proc.wait()
        self.say(f"{self.stamp()} exit code {self.proc.returncode}")
        ids: set[str] = set()
        for e in self.events:
            body = e.get("result") or e.get("step_update") or {}
            found = e.get("conversation_id") or body.get("conversation_id")
            if isinstance(found, str):
                ids.add(found)
        self.say("conversation ids:", sorted(ids))
        self.report.close()
        (self.out / "stdout").write_text("".join(self.lines))
        for name in ("report", "stdout", "stderr"):
            print(f"--- {name} ---")
            print((self.out / name).read_text(), end="")


if __name__ == "__main__":
    Probe(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None).run()
