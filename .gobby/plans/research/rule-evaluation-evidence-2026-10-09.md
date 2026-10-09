<!-- markdownlint-disable MD013 -->

# Rule evaluation hot-path evidence (2026-10-09)

Evidence for `.gobby/plans/rust-rule-evaluation.md` (task #22946). It replaces
the 2026-09-26 Researcher figures, which LM7 ruled stale. All collection was
read-only: retained py-spy captures, the existing rule allow audit log, the
bundled rule templates, and one MCP read of installed rule rows. No load was
generated, no daemon was restarted, and no instrumentation was added. Each
section below embeds the exact script and its verbatim output, so every
figure can be re-derived while its inputs are retained.

## Inputs

- **GIL captures.** `/var/tmp/py-spy/run-*/profile.txt`, written by
  `/usr/local/sbin/py-spy-record` (`py-spy record --pid P --duration S --rate 100
  --gil --format raw`). 155 captures, 2026-10-06 22:37 to 2026-10-08 18:56 CDT,
  644,314 samples, taken mostly by the #23359 stability lane. `--gil` samples
  only the thread holding the GIL. Capture PIDs and load are not recorded per
  file (mtime only), and the window mixes #23359's before, after, and light-load
  runs, so per-capture percentages are the comparable unit.
- **Allow audit.** `~/.gobby/logs/rule-allow-audit.jsonl`, 244,839 lines,
  2026-10-07 06:01 to 2026-10-09 00:15 UTC. `record_rule_evaluation`
  (`src/gobby/telemetry/rule_allow_audit.py`) writes one line per rule whose
  rule-level `when` matched and that did not block.
  `EvaluationMixin._run_rule_loop_pass` (`src/gobby/workflows/engine/evaluation.py`)
  skips non-matching rules before recording them. Blocks and zero-match events
  are absent from this log.
- **Rule corpus.** `src/gobby/install/shared/workflows/rules/` at `0.5.0`: 219
  rule definitions in 140 YAML files. Installed rows, read on 2026-10-09 with
  `gobby-workflows:list_rules(brief=true)`, number 222: 215 enabled and 7
  disabled. The 7 are `block-docker-policy-edits`,
  `inject-plan-enhancer-lessons`, `inject-planner-lessons`,
  `inject-plan-reviewer-lessons`, `inject-qa-reviewer-lessons`,
  `inject-review-lessons-for-touched-files`, and `require-epic-tree-close`.
  146 enabled rows are `before_tool`. `rtk-command-rewrite` ships
  `enabled: false` and is enabled in the installed row by installer consent.
- **Unavailable.** Slow-hook phase logs ended with commit `2fd5eff8e4` (#22866,
  2026-10-01): `mcp.log` holds 773 `Slow hook execution` records, the last at
  2026-10-02 10:33. `rule_evaluations_total` and `hook_phase_duration_seconds`
  are exposed only by `/api/admin/metrics`, which needs the daemon credential
  this research seat must not read. No current source counts total rule passes.

## GIL share by bucket

Buckets: a stack is `rule` when it contains `gobby/workflows/engine/`,
`safe_evaluator`, or `condition_helpers`. It is `hook-other` when it contains
`gobby/hooks/` or `servers/routes/mcp/hooks` and has no rule frame. A stack is
on the loop thread when it contains `gobby/runner.py`. Sub-buckets take the
first match in table order.

```python
"""Second pass: time range, per-capture rule share distribution, thread split, sub-buckets.

Read-only. Usage: python gil_attrib2.py /var/tmp/py-spy
"""

import os
import statistics
import sys
import time
from collections import Counter

ROOT = sys.argv[1]
RULE = ("gobby/workflows/engine/", "safe_evaluator", "condition_helpers")
HOOK = ("gobby/hooks/", "servers/routes/mcp/hooks")

# Sub-buckets inside rule-engine stacks: first match on the deepest-first frame scan.
RULE_SUB = [
    ("db_pool_and_deadline", ("storage/hub/postgres_pool", "transaction_deadline", "psycopg")),
    ("shell_normalization", ("_normalization_shell", "_normalization_", "_path_scope", "_inline_interpreter", "_python_pipeline_classifier")),
    ("condition_eval", ("safe_evaluator", "condition_helpers")),
    ("command_matching", ("command_matching",)),
    ("selectors", ("workflows/selectors",)),
    ("templates", ("workflows/templates", "engine/templating")),
    ("subprocess_spawn", ("utils/spawn", "run_command")),
    ("telemetry", ("gobby/telemetry",)),
    ("effects", ("engine/effects", "mcp_injections", "proxy_hooks")),
    ("loop_scaffold", ("_run_rule_loop_pass", "_run_rule_loop_worker", "engine/_offload", "asyncio/")),
]
HOOK_SUB = [
    ("session_activation", ("hooks/session_activation",)),
    ("transcripts", ("sessions/transcripts", "transcript")),
    ("inbox_envelope", ("hooks/inbox", "envelope_dedupe")),
    ("db_pool_and_deadline", ("storage/hub/postgres_pool", "transaction_deadline", "psycopg")),
    ("normalization", ("_normalization_", "_path_scope")),
    ("state_manager_vars", ("workflows/state_manager",)),
    ("hook_manager", ("hooks/hook_manager",)),
    ("adapter", ("adapter_execution", "adapters/")),
]


def sub_of(stack: str, table) -> str:
    for name, needles in table:
        if any(n in stack for n in needles):
            return name
    return "other"


rows = []
rule_sub = Counter()
hook_sub = Counter()
rule_main = rule_worker = 0
hook_main = hook_worker = 0
for d in os.listdir(ROOT):
    path = os.path.join(ROOT, d, "profile.txt")
    try:
        mtime = os.path.getmtime(path)
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        continue
    total = rule = hook = 0
    for line in text.splitlines():
        stack, _, n = line.rpartition(" ")
        try:
            count = int(n)
        except ValueError:
            continue
        total += count
        main = "gobby/runner.py" in stack
        if any(x in stack for x in RULE):
            rule += count
            rule_sub[sub_of(stack, RULE_SUB)] += count
            if main:
                rule_main += count
            else:
                rule_worker += count
        elif any(x in stack for x in HOOK):
            hook += count
            hook_sub[sub_of(stack, HOOK_SUB)] += count
            if main:
                hook_main += count
            else:
                hook_worker += count
    if total:
        rows.append((mtime, total, rule, hook))

rows.sort()
fmt = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t))
print(f"captures={len(rows)} first={fmt(rows[0][0])} last={fmt(rows[-1][0])}")
days = Counter(time.strftime("%m-%d", time.localtime(r[0])) for r in rows)
print("captures per day:", dict(sorted(days.items())))
tot = [r[1] for r in rows]
rule_pct = [100 * r[2] / r[1] for r in rows]
hook_pct = [100 * r[3] / r[1] for r in rows]
rule_n = [r[2] for r in rows]


def dist(name, xs):
    xs = sorted(xs)
    q = lambda p: xs[min(len(xs) - 1, int(p * len(xs)))]
    print(f"{name:22s} mean={statistics.mean(xs):8.1f} p50={q(0.5):8.1f} p90={q(0.9):8.1f} max={xs[-1]:8.1f}")


dist("samples/capture", tot)
dist("rule samples/capture", rule_n)
dist("rule % of capture", rule_pct)
dist("hook-other %", hook_pct)
print(f"rule on main(loop) thread={rule_main} worker={rule_worker}")
print(f"hook-other on main(loop) thread={hook_main} worker={hook_worker}")
rs = sum(rule_sub.values())
print("\nrule-engine sub-buckets:")
for k, v in rule_sub.most_common():
    print(f"  {k:22s} {v:7d} {100*v/rs:5.1f}% of rule")
hs = sum(hook_sub.values())
print("\nhook-other sub-buckets:")
for k, v in hook_sub.most_common():
    print(f"  {k:22s} {v:7d} {100*v/hs:5.1f}% of hook-other")
```

Output (`python gil_attrib2.py /var/tmp/py-spy`):

```text
captures=155 first=2026-10-06 22:37 last=2026-10-08 18:56
captures per day: {'10-06': 40, '10-07': 92, '10-08': 23}
samples/capture        mean=  4156.9 p50=  3443.0 p90=  9821.0 max= 23719.0
rule samples/capture   mean=   258.2 p50=   196.0 p90=   541.0 max=  1283.0
rule % of capture      mean=     6.3 p50=     5.6 p90=    11.2 max=    18.4
hook-other %           mean=    22.6 p50=    22.5 p90=    31.3 max=    48.3
rule on main(loop) thread=0 worker=40028
hook-other on main(loop) thread=13737 worker=133202

rule-engine sub-buckets:
  loop_scaffold            10228  25.6% of rule
  shell_normalization       9752  24.4% of rule
  db_pool_and_deadline      6423  16.0% of rule
  condition_eval            5039  12.6% of rule
  command_matching          2740   6.8% of rule
  templates                 1503   3.8% of rule
  selectors                 1467   3.7% of rule
  subprocess_spawn           957   2.4% of rule
  effects                    802   2.0% of rule
  other                      654   1.6% of rule
  telemetry                  463   1.2% of rule

hook-other sub-buckets:
  db_pool_and_deadline     49091  33.4% of hook-other
  session_activation       20895  14.2% of hook-other
  inbox_envelope           20839  14.2% of hook-other
  hook_manager             19022  12.9% of hook-other
  other                    17878  12.2% of hook-other
  state_manager_vars        8328   5.7% of hook-other
  normalization             5184   3.5% of hook-other
  transcripts               3584   2.4% of hook-other
  adapter                   2118   1.4% of hook-other
```

Readings:

- Rule work averages 6.3% of each capture's GIL samples (p50 5.6%, p90 11.2%,
  max 18.4%). All 40,028 rule samples are on pool threads; none is on the loop
  thread.
- Other hook-path work averages 22.6% (p90 31.3%), and 13,737 of its samples are
  on the loop thread.
- Inside rule work, rule logic proper (condition evaluation 12.6%, command
  matching 6.8%, templates 3.8%, selectors 3.7%) totals 26.9%. Per-pass event
  loop scaffolding is 25.6%, shell normalization 24.4%, and database pool work
  16.0%.

## Rule GIL share by caller

The rule pass runs under `asyncio.run` on a rule-loop pool thread, and engine
offloads run on the rule-engine pool, so most rule stacks carry no caller frame.

```python
"""Third pass: split rule-engine GIL samples by consumer (caller) frames. Read-only.

Usage: python gil_attrib3.py /var/tmp/py-spy
Rule stacks are those gil_attrib2.py counts (engine/, safe_evaluator, condition_helpers).
The rule pass itself runs under asyncio.run on a rule-loop worker thread, so its stacks
carry no caller frame; those land in "rule_loop_worker (caller not on stack)".
"""
import os, sys
from collections import Counter

ROOT = sys.argv[1]
RULE = ("gobby/workflows/engine/", "safe_evaluator", "condition_helpers")
CONSUMERS = [
    ("mcp_proxy", ("mcp_proxy/services/tool_proxy", "mcp_proxy/services/result_handling")),
    ("web_chat", ("servers/websocket/chat",)),
    ("agent_runner", ("servers/_app_lifecycle", "agents/runner")),
    ("hooks_route", ("servers/routes/mcp/hooks", "hooks/hook_manager", "workflows/hooks.py")),
    ("rule_loop_worker (caller not on stack)", ("_run_rule_loop_worker", "_run_rule_loop_pass")),
    ("rule_engine_pool offload (caller not on stack)", ("engine/_offload",)),
]
by = Counter(); total_rule = 0
for d in os.listdir(ROOT):
    path = os.path.join(ROOT, d, "profile.txt")
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        continue
    for line in text.splitlines():
        stack, _, n = line.rpartition(" ")
        try:
            count = int(n)
        except ValueError:
            continue
        if not any(x in stack for x in RULE):
            continue
        total_rule += count
        for name, needles in CONSUMERS:
            if any(x in stack for x in needles):
                by[name] += count
                break
        else:
            by["other"] += count
print(f"rule samples={total_rule}")
for k, v in by.most_common():
    print(f"  {k:48s} {v:7d} {100*v/total_rule:5.1f}%")
```

```text
rule samples=40028
  rule_loop_worker (caller not on stack)             25394  63.4%
  other                                               8115  20.3%
  hooks_route                                         6494  16.2%
  rule_engine_pool offload (caller not on stack)        25   0.1%
```

The `hooks_route` bucket matched on `workflows/hooks.py`. That frame belongs
to `WorkflowHookHandler`, which both the hooks route and the MCP proxy call.
A follow-up count over rule stacks without rule-loop frames found 7 samples
carrying `hooks/hook_manager` or `servers/routes/mcp/hooks` frames and 5,602
carrying only `workflows/hooks.py`. No rule sample carries an MCP proxy
(`mcp_proxy/services/tool_proxy`, `result_handling`), web chat
(`servers/websocket/chat`), or in-process agent runner (`servers/_app_lifecycle`,
`agents/runner`) frame. The split of rule work between consumers is therefore
unmeasured.

The `other` bucket's heaviest stacks begin in
`EnforcementCheckMixin._check_step_tool_enforcement` and
`EnforcementCheckMixin._get_step_for_session`
(`src/gobby/workflows/engine/enforcement_checks.py`),
`RuleEngine._filter_by_active_rules` with `rule_matches_agent`,
`DeliveryFormattingMixin._format_memory_backed_result`, and
`EnforcementAuditMixin._audit_step_tool_call`.

## Engine component shares (non-exclusive)

```python
"""Fourth pass: non-exclusive 'contains' shares of rule-engine GIL samples. Read-only.

Usage: python gil_attrib4.py /var/tmp/py-spy
"""
import os, sys
from collections import Counter

ROOT = sys.argv[1]
RULE = ("gobby/workflows/engine/", "safe_evaluator", "condition_helpers")
MARK = {
    "step_tool_enforcement (enforcement_checks.py)": "engine/enforcement_checks",
    "agent_step_instances lookups (step_instances.py)": "workflows/step_instances",
    "memory-index delivery (delivery_formatting/injection_tracking)": "engine/delivery_formatting",
    "selectors (rule_matches_agent)": "workflows/selectors",
    "workflow audit writes (workflow_audit.py)": "storage/workflow_audit",
    "task helpers (storage/tasks)": "storage/tasks",
    "shared WorkflowHookHandler frame (workflows/hooks.py)": "workflows/hooks.py",
    "hook_manager or hooks route frame": "hooks/hook_manager",
    "rule loop pass (_run_rule_loop_pass)": "_run_rule_loop_pass",
}
c = Counter(); total = 0
for d in os.listdir(ROOT):
    try:
        text = open(os.path.join(ROOT, d, "profile.txt"), encoding="utf-8", errors="replace").read()
    except OSError:
        continue
    for line in text.splitlines():
        stack, _, n = line.rpartition(" ")
        try:
            count = int(n)
        except ValueError:
            continue
        if not any(x in stack for x in RULE):
            continue
        total += count
        for k, needle in MARK.items():
            if needle in stack:
                c[k] += count
print(f"rule samples={total}")
for k, v in c.most_common():
    print(f"  {k:62s} {v:7d} {100*v/total:5.1f}%")
```

```text
rule samples=40028
  rule loop pass (_run_rule_loop_pass)                             23396  58.4%
  shared WorkflowHookHandler frame (workflows/hooks.py)             6487  16.2%
  step_tool_enforcement (enforcement_checks.py)                     4514  11.3%
  agent_step_instances lookups (step_instances.py)                  2202   5.5%
  selectors (rule_matches_agent)                                    1467   3.7%
  memory-index delivery (delivery_formatting/injection_tracking)     800   2.0%
  workflow audit writes (workflow_audit.py)                          428   1.1%
  task helpers (storage/tasks)                                       211   0.5%
  hook_manager or hooks route frame                                    7   0.0%
```

Step-workflow tool enforcement runs inside `RuleEngine.evaluate` and appears in
11.3% of rule samples, with agent step instance lookups in 5.5%.

## Matched rule passes in the allow audit

Lines of one pass are published as one batch on the daemon loop, so the script
treats consecutive lines with the same session and event and a gap under 2 ms as
one pass. A rule counts as mutating when its bundled definition carries a
`set_variable`, `mcp_call`, `run_command`, `load_skill`, `inject_context`, or
`proxy_hook` effect. Effect-level `when` clauses are not visible in the log, so
the mutating share is an upper bound at rule level. An optional trailing argument
names rules to drop before grouping.

```python
"""Group rule-allow-audit lines into rule passes and classify matched rules. Read-only.

Usage: python allow_audit_passes.py <audit.jsonl> <rules-dir>
A line is written for each rule whose rule-level `when` matched and that did not block
(evaluation.py _run_rule_loop_pass; non-matching rules `continue` before recording).
Lines of one pass are published as one batch on the loop, so consecutive lines with the
same (session_id, event) and a timestamp gap under 2 ms are treated as one pass.
"""
import collections, json, os, sys, yaml
from datetime import datetime

AUDIT, RULES = sys.argv[1], sys.argv[2]
EXCLUDE = set(sys.argv[3:])  # rule names to drop before grouping
MUTATING = {"set_variable", "mcp_call", "run_command", "load_skill", "inject_context", "proxy_hook"}
effects = {}
for d, _, fs in os.walk(RULES):
    for f in fs:
        if f.endswith(".yaml"):
            doc = yaml.safe_load(open(os.path.join(d, f), encoding="utf-8")) or {}
            for name, rule in (doc.get("rules") or {}).items():
                rule = rule or {}
                effs = rule.get("effects") or ([rule["effect"]] if rule.get("effect") else [])
                effects[name] = {e.get("type") for e in effs if isinstance(e, dict)}

passes = collections.Counter(); mut_passes = collections.Counter(); rule_passes = collections.Counter()
lat = collections.defaultdict(list); unknown = collections.Counter()
first = last = None; lines = 0
cur_key = cur_ts = None; cur_rules = []

def close():
    if cur_key is None:
        return
    ev = cur_key[1]
    passes[ev] += 1
    if any((effects.get(r) or set()) & MUTATING for r in cur_rules):
        mut_passes[ev] += 1
    for r in set(cur_rules):
        rule_passes[(ev, r)] += 1

for raw in open(AUDIT, encoding="utf-8", errors="replace"):
    try:
        rec = json.loads(raw)
    except ValueError:
        continue
    lines += 1
    ts = datetime.fromisoformat(rec["timestamp"]).timestamp()
    first = first or rec["timestamp"]; last = rec["timestamp"]
    key = (rec["session_id"], rec["event"])
    name = rec["rule_name"]
    if name in EXCLUDE:
        continue
    if name not in effects:
        unknown[name] += 1
    lat[rec["event"]].append(rec.get("latency_ms") or 0.0)
    if key == cur_key and ts - cur_ts < 0.002:
        cur_rules.append(name)
    else:
        close(); cur_key, cur_rules = key, [name]
    cur_ts = ts
close()

print(f"lines={lines} window={first} .. {last}")
for ev, n in passes.most_common():
    print(f"{ev:14s} passes-with-a-matched-allow={n:7d} with-mutating-matched-rule={mut_passes[ev]:7d} ({100*mut_passes[ev]/n:5.1f}%)")
print("\ntop matched rules per event (share of that event's passes):")
for ev, n in passes.most_common():
    top = sorted(((c, r) for (e, r), c in rule_passes.items() if e == ev), reverse=True)[:6]
    print(f" {ev}: " + "; ".join(f"{r} {100*c/n:.0f}% {sorted(effects.get(r) or {'?'})}" for c, r in top))
print("\nnot in bundled templates:", dict(unknown.most_common(8)))
for ev, xs in lat.items():
    xs.sort(); q = lambda p: xs[min(len(xs)-1, int(p*len(xs)))]
    print(f"latency_ms {ev:14s} p50={q(.5):.3f} p90={q(.9):.3f} p99={q(.99):.3f} max={xs[-1]:.1f}")
```

Output (`python allow_audit_passes.py ~/.gobby/logs/rule-allow-audit.jsonl src/gobby/install/shared/workflows/rules`):

```text
lines=244839 window=2026-10-07T06:01:23.476590+00:00 .. 2026-10-09T00:15:10.563741+00:00
before_tool    passes-with-a-matched-allow=  50339 with-mutating-matched-rule=  49007 ( 97.4%)
after_tool     passes-with-a-matched-allow=  25656 with-mutating-matched-rule=  25564 ( 99.6%)
before_agent   passes-with-a-matched-allow=  20790 with-mutating-matched-rule=  20790 (100.0%)
stop           passes-with-a-matched-allow=    983 with-mutating-matched-rule=    881 ( 89.6%)
session_start  passes-with-a-matched-allow=    943 with-mutating-matched-rule=    943 (100.0%)
pre_compact    passes-with-a-matched-allow=    610 with-mutating-matched-rule=    610 (100.0%)

top matched rules per event (share of that event's passes):
 before_tool: rtk-command-rewrite 97% ['proxy_hook']; track-code-index-attempts 26% ['set_variable']; block-gobby-tasks-cli 4% ['block']; require-pytest-guard-env 4% ['block']; no-remote-exec 4% ['block']; no-full-vitest-suite 4% ['block']
 after_tool: track-code-index-navigation 47% ['set_variable']; track-turn-written-paths 24% ['set_variable']; inject-tool-error-recovery 11% ['inject_context', 'set_variable']; nudge-compact-on-context-pressure-mid-turn 6% ['inject_context', 'load_skill']; mark-gobby-session-feedback-submitted 4% ['set_variable']; track-ocr-review-freshness 3% ['set_variable']
 before_agent: snapshot-mcp-proxy-ready-on-turn-start 100% ['set_variable']; reset-code-index-navigation 100% ['set_variable']; surface-memories-on-turn-start 95% ['mcp_call', 'set_variable']; increment-parent-turn-seq 95% ['set_variable']; bootstrap-default-agent-core-skills 42% ['load_skill']; remind-brevity-on-turn-start 13% ['inject_context', 'set_variable']
 stop: note-mcp-proxy-missed-turn 66% ['set_variable']; detect-brevity-contrastive-drift 21% ['set_variable']; pending-handoff-delivery 12% ['?']; check-memory-guidance-on-initial-stop 5% ['block', 'set_variable']; impeccable-deep-pass 0% ['run_command', 'set_variable']; detect-brevity-literal-drift 0% ['set_variable']
 session_start: rearm-close-gates-on-session-start 100% ['set_variable']; inject-task-context-on-start 91% ['inject_context']; reset-plan-mode-on-session-start 90% ['set_variable']; reset-skill-injection 65% ['set_variable']; reset-seat-common-on-context-loss 65% ['set_variable']; reset-progressive-discovery 65% ['set_variable']
 pre_compact: preserve-context-on-compact 100% ['set_variable']

not in bundled templates: {'pending-handoff-delivery': 121}
latency_ms before_tool    p50=0.006 p90=0.214 p99=9.168 max=8420.5
latency_ms before_agent   p50=0.445 p90=229.049 p99=2547.547 max=12012.5
latency_ms after_tool     p50=0.039 p90=0.223 p99=944.086 max=8705.1
latency_ms pre_compact    p50=0.096 p90=0.159 p99=0.366 max=3.1
latency_ms session_start  p50=0.011 p90=0.555 p99=1.302 max=39.1
latency_ms stop           p50=0.016 p90=0.181 p99=0.556 max=4.6
```

Output with `rtk-command-rewrite` dropped (same command plus
`rtk-command-rewrite`; this run read the log about one minute later, to
00:16:17 UTC):

```text
lines=244983 window=2026-10-07T06:01:23.476590+00:00 .. 2026-10-09T00:16:17.441657+00:00
after_tool     passes-with-a-matched-allow=  25674 with-mutating-matched-rule=  25582 ( 99.6%)
before_agent   passes-with-a-matched-allow=  20809 with-mutating-matched-rule=  20809 (100.0%)
before_tool    passes-with-a-matched-allow=  14970 with-mutating-matched-rule=  13125 ( 87.7%)
stop           passes-with-a-matched-allow=    983 with-mutating-matched-rule=    881 ( 89.6%)
session_start  passes-with-a-matched-allow=    943 with-mutating-matched-rule=    943 (100.0%)
pre_compact    passes-with-a-matched-allow=    611 with-mutating-matched-rule=    611 (100.0%)

top matched rules per event (share of that event's passes):
 after_tool: track-code-index-navigation 47% ['set_variable']; track-turn-written-paths 24% ['set_variable']; inject-tool-error-recovery 11% ['inject_context', 'set_variable']; nudge-compact-on-context-pressure-mid-turn 6% ['inject_context', 'load_skill']; mark-gobby-session-feedback-submitted 4% ['set_variable']; track-ocr-review-freshness 3% ['set_variable']
 before_agent: snapshot-mcp-proxy-ready-on-turn-start 100% ['set_variable']; reset-code-index-navigation 100% ['set_variable']; surface-memories-on-turn-start 95% ['mcp_call', 'set_variable']; increment-parent-turn-seq 95% ['set_variable']; bootstrap-default-agent-core-skills 42% ['load_skill']; remind-brevity-on-turn-start 13% ['inject_context', 'set_variable']
 before_tool: track-code-index-attempts 87% ['set_variable']; block-gobby-tasks-cli 12% ['block']; require-pytest-guard-env 12% ['block']; no-remote-exec 12% ['block']; no-full-vitest-suite 12% ['block']; no-full-pytest-suite 12% ['block']
 stop: note-mcp-proxy-missed-turn 66% ['set_variable']; detect-brevity-contrastive-drift 21% ['set_variable']; pending-handoff-delivery 12% ['?']; check-memory-guidance-on-initial-stop 5% ['block', 'set_variable']; impeccable-deep-pass 0% ['run_command', 'set_variable']; detect-brevity-literal-drift 0% ['set_variable']
 session_start: rearm-close-gates-on-session-start 100% ['set_variable']; inject-task-context-on-start 91% ['inject_context']; reset-plan-mode-on-session-start 90% ['set_variable']; reset-skill-injection 65% ['set_variable']; reset-seat-common-on-context-loss 65% ['set_variable']; reset-progressive-discovery 65% ['set_variable']
 pre_compact: preserve-context-on-compact 100% ['set_variable']

not in bundled templates: {'pending-handoff-delivery': 121}
latency_ms before_tool    p50=0.003 p90=0.649 p99=20.226 max=8420.5
latency_ms before_agent   p50=0.445 p90=228.939 p99=2542.929 max=12012.5
latency_ms after_tool     p50=0.039 p90=0.223 p99=944.086 max=8705.1
latency_ms pre_compact    p50=0.095 p90=0.158 p99=0.366 max=3.1
latency_ms session_start  p50=0.011 p90=0.555 p99=1.302 max=39.1
latency_ms stop           p50=0.016 p90=0.181 p99=0.556 max=4.6
```

Readings:

- Among passes where any rule matched and allowed, a mutating rule also matched in
  97.4% of `before_tool`, 99.6% of `after_tool`, and 100% of `before_agent`
  (turn start), `session_start`, and `pre_compact` passes.
- `rtk-command-rewrite` (no `when`, `tools: [Bash]`, `proxy_hook` effect) matches
  every Bash call. Without it, `before_tool` passes with a matched allow fall
  from 50,339 to 14,970. At least 35,369 Bash calls matched only that rule.
- Turn-start rule latency (`before_agent`) reaches p90 229 ms and p99 2,548 ms.
  `surface-memories-on-turn-start` carries an `mcp_call` effect on 95% of those
  passes.
- These figures bound matched passes only. Zero-match events and blocks are not
  in the log, so the share of events no rule matches is unmeasured.

## Cross-family seams per rule

A rule needs a seam when its definition text names a helper or effect type
below; every rule also reads event data and session variables.

```python
"""Per-rule seam count over bundled rule templates. Read-only.

Usage: python rule_seams_per_rule.py <rules-dir>
A rule needs a seam when its YAML text names a helper or effect type below.
"""
import os, re, sys, collections, yaml

ROOT = sys.argv[1]
SEAMS = {
    "tasks": ["all_tasks_have_label", "task_tree_complete", "task_needs_human_review", "task_state_in",
              "task_type_in", "creates_task_outside_lane", "open_lane_epic_refs",
              "claimed_task_acceptance_test_paths", "task_commit_project_path_allowlist_violation",
              "missing_claimed_task_extra_skills"],
    "sessions_agents": ["send_keys_target_in_scope", "send_message_target_allowed", "spawn_target_allowed",
                        "network_override_allowed", "has_pending_messages", "pending_message_count",
                        "has_active_agent_wait", "has_active_coordination_wait", "has_durable_stop_wait",
                        "has_stop_signal"],
    "transcript_turn": ["assistant_response_matches_any", "paths_written_this_turn"],
    "code_index": ["navigation_requires_index", "shell_command_invokes_gcode"],
    "filesystem_monolith": ["projected_monolith_paths", "outstanding_monolith_paths"],
}
EFFECTS = {"mcp_call", "run_command", "proxy_hook", "load_skill"}

def effect_types(rule):
    out = set()
    effs = rule.get("effects") or ([rule["effect"]] if rule.get("effect") else [])
    for e in effs:
        if isinstance(e, dict) and e.get("type"):
            out.add(e["type"])
    return out

per = collections.Counter(); eff_ct = collections.Counter(); any_seam = 0; total = 0
for d, _, fs in os.walk(ROOT):
    for f in fs:
        if not f.endswith(".yaml"):
            continue
        doc = yaml.safe_load(open(os.path.join(d, f), encoding="utf-8"))
        rules = doc.get("rules") or {}
        items = rules.items() if isinstance(rules, dict) else ((r.get("name"), r) for r in rules)
        for name, rule in items:
            rule = rule or {}
            total += 1
            text = yaml.safe_dump(rule)
            hit = False
            for seam, needles in SEAMS.items():
                if any(re.search(r"\b" + re.escape(n) + r"\b", text) for n in needles):
                    per[seam] += 1; hit = True
            for t in effect_types(rule):
                eff_ct[t] += 1
                if t in EFFECTS:
                    per["effect:" + t] += 1; hit = True
            any_seam += hit
print(f"rules={total} with_cross_family_seam={any_seam} pure={total-any_seam}")
for k, v in sorted(per.items()):
    print(f"  {k}: {v}")
print("effect types (rules carrying each):", dict(eff_ct.most_common()))
```

```text
rules=219 with_cross_family_seam=44 pure=175
  code_index: 2
  effect:load_skill: 4
  effect:mcp_call: 12
  effect:proxy_hook: 1
  effect:run_command: 2
  filesystem_monolith: 4
  sessions_agents: 10
  tasks: 8
  transcript_turn: 2
effect types (rules carrying each): {'block': 151, 'set_variable': 49, 'inject_context': 18, 'mcp_call': 12, 'load_skill': 4, 'run_command': 2, 'observe': 1, 'proxy_hook': 1}
```

Of 219 bundled rules, 175 need only the event and session variables. 44 need at
least one other seam.

## Condition and template surface

- `build_condition_helpers()` (`src/gobby/workflows/safe_evaluator.py`) returns
  54 names, builtins included. `TemplatingMixin._build_allowed_funcs`
  (`src/gobby/workflows/engine/templating.py`) assigns 20 more entries, 19 of
  them new names (`isinstance` is already present). Read with
  `uv run python -c "from gobby.workflows.safe_evaluator import build_condition_helpers; print(sorted(build_condition_helpers()))"`.
- `SafeExpressionEvaluator` accepts these `ast` nodes: `Expression`, `BoolOp`
  (`And`, `Or`), `BinOp` (`Add`, `Sub`, `Mod`, `FloorDiv`), `UnaryOp` (`Not`,
  `UAdd`, `USub`), `Compare` (`Eq`, `NotEq`, `Lt`, `LtE`, `Gt`, `GtE`, `In`,
  `NotIn`, `Is`, `IsNot`), `IfExp`, `Call`, `Attribute`, `Subscript`, `Name`,
  `Constant`, `List`, `Tuple`, `Dict`, `ListComp`, and `GeneratorExp`.
- The 219 bundled rules hold 130 Jinja blocks. Tags used are `set`, `if`, `elif`,
  `else`, and `for`. Filters used are `join` (9), `truncate` (2), `list` (2),
  `shlex_quote` (1, custom in `src/gobby/workflows/templates.py`), `int` (1), and
  `tojson` (1). Templates also call `.get` and `.values` on mappings.
  `TemplateEngine` registers the custom filters `regex_replace`, `regex_search`,
  and `shlex_quote`.
- Rule bodies are stored as JSONB in `rule_definitions.definition_json`
  (`crates/gcore/assets/schema/baseline.sql`), so a Rust reader needs no YAML.

## Shell normalization surface

The rule engine imports these modules from `src/gobby/hooks/`:
`_normalization_shell` (970 lines), `_normalization_segments` (111), `_ansi_c`,
`_path_scope` (289), `_python_pipeline_classifier` (992), and
`_normalization_tools` (197). The importers are `command_matching.py`,
`commit_guard.py`, `condition_helpers_paths.py`, `evaluation.py`,
`run_command.py`, `validation_cover.py`, `code_navigation_recovery.py`, and
`provider_launch_guard.py`. Hook ingress normalization uses the same modules: the
`normalization` sub-bucket is 3.5% of hook-other samples.
