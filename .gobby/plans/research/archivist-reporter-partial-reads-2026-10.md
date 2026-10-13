<!-- Recorded verbatim by Plan Writer gobby#16490 for #24260 from Code Reviewer gobby#16032's message 551afc55 (resent as 5ea38a31-2f65-4aa4-8e29-95cc27e7dfb7, 2026-10-13). -->

#24260 findings: why the Archivist digest and Reporter produce partial reads (Code 16032, read-only)

Delivery: my code-reviewer step machine blocks claim_task while #24232 (Archivist 'Claude' label fix) is in the verdict step, and agent enforcement blocks Write for code-reviewer. So this message is the deliverable, per your fallback; please record it.

Sources:
- Definitions and code are cited at 0.5.0 tip a2ae38ee.
- Five Reporter run transcripts: f0897544 (00:02 CT 10-13), 53b89bf6 (23:03 CT), 8b04debc (21:35 CT), b8b952ab (20:36 CT), 5d08feb4 (20:08 CT).
- The 10-12 Desktop digest.
- The operator list relayed in session 2c0e9ec2.

## Root causes

1. Backlog shown as a lower bound ("at least 14", "≥12")
- Code: _listing.py:110-113 closes each compact page at 14,000 bytes. Every row also carries all close receipts with full facts (_listing.py:101-104). So limit=100 returns only 3-8 rows per page.
- Code: compact pages carry no total. open_count exists only with parent_task_id (_listing.py:245-256).
- Definition: reporter.yaml:38 caps gathering at 240 s and 35 tool calls. The Reporter pages four lists (claimed, escalated, closed, candidate) and runs out of calls before next_offset reaches null.
- Evidence: in f0897544 the candidate list paged through offsets 0, 3, 4, 7, 11 and 15, then stopped ("≥12/10 ... read to offset 15"). 5d08feb4 reported "at least 12 of 10 (partial count)".

2. Decision items (~14 KB) unread
- Code: reporter_sources.py:106-107 refuses the whole section once its JSON reaches 14,000 bytes. reporter_sources.py:54-62 returns the three decision headings as raw bodies with no item paging.
- Definition: reporter.yaml:44 makes an oversized section a gap and forbids byte slices, so nothing else can be read.
- Growth: archivist.yaml:24-25 carries content forward. The 10-12 digest is 492,945 bytes: Decisions needed 9,101 bytes (45 items), Operator actions 1,376, Gaps 4,055.
- Evidence: 53b89bf6 and 8b04debc both got "Selected digest section exceeds 14KB; report source gap".

3. Dependency list lost (timeout or offload), with no retry or gap marker
- Code: list_blocked_tasks (task_readiness.py:499-523) has default limit 20, no offset, no next_offset and no total, and returns full to_brief rows. Storage already supports offset (_queries.py:599-605).
- Code: get_tool_result pages are at most 7,488 chars (features.py:132: max_envelope_chars 8,000 minus 512), so an 85K result needs 12 or more calls.
- Definition: reporter.yaml:38 says "Do not wait for seats, repeatedly poll" and has no retry rule for a failed daemon call.
- Evidence, offload: with limit=50 the result was offloaded at 85,658 chars (f0897544) and 173,251 chars (5d08feb4). f0897544 sampled one search_tool_result and reported "Story B dependency refs not resolved".
- Evidence, timeout: 8b04debc started at the tail of the 21:25-21:30 cutover and got "control plane unavailable ... health check did not respond within 2s". gobby status timed out, and the run never called list_blocked_tasks.
- Contributing, not observed in these runs: the stdio proxy's default per-call timeout is 30 s (stdio_proxy.py:385).

4. Git tip contradiction ("Git only shows 0034703982" while #24219 had landed at 0742eda03c)
- Definition: reporter.yaml:47's only git read is `git log 0.5.0 --since="30 minutes ago" -n 50`. Nothing reads the tip, and "landed" comes from receipt recorded_at (reporter.yaml:46). The two windows disagree.
- Reflog: 0742eda03c (#24219 nested escaped backticks landing) at 23:04:02, then 0034703982 (#24224 coverage manifests, Plan Writer commit) at 23:13:28.
- Inference: a run between 23:34 and 23:43 would see only 0034703982 in the window while listing #24219 from receipts.
- The digest also relays tips from Orchestrator text (10-12 digest line 700: "fails on tip 0034703982").

5. Stale decision items with no age, including decided ones
- Definition: archivist.yaml:24-25 carries items forward "until it's resolved", but nothing removes decided items or requires a date per item.
- Definition: reporter.yaml:60 lists decisions from that text with no age or resolved check.
- Evidence: the 10-12 Decisions needed has 45 items. About 40 are Decided, RESOLVED or DONE; 4-5 are open or unknown; only 6 carry a date or time.

## Additional causes found
- Date rollover: reporter_sources.py:93-94 reads only today's digest, and reporter.yaml:44 says "Do not read previous digests". At 00:02 CT, f0897544 found no 10-13 file, so queues, heavy slots, holds, decision links and the Consolidator section were all unknown. #24232's path move keeps this behavior.
- Self-truncation: f0897544 and 8b04debc piped projections through head -c 2000/8000/20000, which reporter.yaml:44 forbids. 8b04debc also used `timeout`, which macOS lacks.

## Recommended fixes (least mechanism)

Pagination to completion
- Code: compact list_tasks returns total on every page. Reuse count_tasks (_listing.py:246) extended to the claimed, escalated, candidate and updated_since filters. The backlog is then exact from page 1.
- Code: compact rows keep receipt kind, SHA and recorded_at, and move facts behind a get_task pointer, like the existing elision. This roughly triples rows per page.
- Definition (reporter.yaml:38, :46):
  - Exempt next_offset continuations from the 35-call cap.
  - State a count as exact only when rows read equal total; otherwise write "N of total read".
  - Page one list to completion before starting the next.

Size limits and offloaded-result paging
- Code: list_blocked_tasks gets view="compact", offset, next_offset and total, reusing compact_tasks.
- Code: reporter_sources pages instead of refusing. decisions returns one entry per bullet, with --offset and next_offset, each page under 14,000 bytes.
- Definition: page any offloaded list you count with get_tool_result to null; never sample it with search_tool_result.

Timeout retry with gap marker
- Definition (reporter.yaml:38):
  - Retry a failed daemon call or health check once, after 30-60 s, within the 900 s timeout.
  - If it fails again, write "GAP: <source> - <error>" in that slot.
  - If a BEFORE notice is live with no DAEMON BACK yet, wait within the budget, then report the hold.
- No code needed.

One source of truth for operator decisions
- Definition:
  - The decision list comes from open tasks labeled needs-decision, or escalated awaiting the operator, via list_tasks(..., closed=false, view="compact").
  - escalated_at gives each item's age; a closed or de-escalated task counts as resolved.
  - The digest supplies only links.
  - Archivist "Decisions needed" holds open items only, one line each: ref, phrase, opened time, link. Decided items are dropped the same day.
- Code: nothing beyond the total and compact fixes above.

Git tip freshness
- Definition (reporter.yaml:47):
  - Read `git rev-parse --short 0.5.0` and `git log -1 --format='%h %cI' 0.5.0` at report time; state the tip only from these.
  - "Landed in last 30 min" comes from landing receipts, each checked with `git merge-base --is-ancestor <sha> 0.5.0`.
  - Never take a tip from the digest or receipt facts.
- Definition (archivist.yaml:23): label relayed tips "reported tip at HH:MM".

Rollover and truncation
- Code: when today's digest is absent, reporter_sources falls back to the latest prior digest, labeled with its date and age as stale.
- Definition: run the projections bare, with no head, timeout or pipes.

Split
- Code:
  - _listing.py: total, and slimmer receipts in compact rows.
  - list_blocked_tasks: compact view with paging and total.
  - reporter_sources: paging and rollover fallback.
- Definition text: reporter.yaml:38, :44, :46, :47 and :60; archivist.yaml:23-25.
