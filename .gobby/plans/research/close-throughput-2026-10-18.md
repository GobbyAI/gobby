# Close throughput, 18 October 2026

## Decision and status

Activation batching and repeated validation deserve attention before increasing review concurrency. In the verified cohort, completed landing-to-activation intervals account for **2,696.42 task-minutes (44.94 task-hours)** within today's window. Recorded independent approval-to-landing-receipt latency adds **393.65 task-minutes**. Seven recovered close-review runs consume **74.74 run-minutes**. These are measured stage durations, not an estimate that all of that time was waste.

**Evidence-bounded research artifact for #24721 Close-throughput research.** The available read surfaces do not prove a complete close-attempt census or an exclusive ranking of avoidable lost time. This report identifies 23 tasks with closure or attempt evidence, gives the observable intervals, and marks the rest unobservable. It must not be represented as a complete-day measurement. ORCH's 17:29 CDT ruling, relayed in message `2515047f-5804-40c9-b6a0-be49c8cc043e`, confirms no durable close-attempt read/export surface is known and directs shipment with this gap as a finding.

Snapshot: **2026-10-18 00:00–17:10 CDT**, equivalently **05:00–22:10 UTC**. Tables use UTC and minutes. The day remains in progress. Counts below stop at that cutoff; later closes are outside this report.

## Evidence and method

- Read-only task MCP inventory: 133 rows returned by `list_tasks(updated_since="2026-10-18T05:00:00Z", sort_by="updated_at", view="compact")`, exhausted by `next_offset`. Full records were recovered for 38 relevant tasks, with #20438 Temp-root docs refreshed after closure.
- RM sent-history exports: `3cc6989c-d234-4041-991b-f30eb6ed6a0e`, `fa50c050-a2f9-43c8-b784-d5169303d1d9`, `14cf1f8a-d957-4608-9239-4adf6f9a0e52`, `c1280f53-cbc0-49e3-80c6-10ed79c265b3`, and gap overlap `56380042-3fde-464f-bd6d-b6188144a804`. Reassembled bounded result slices yielded **2,501 distinct message IDs / 1,624 distinct message bodies** in today's range, through 22:02:40 UTC. Later completion evidence comes from the seven agent-run reads and the received RM close update for #20438 Temp-root docs.
- Deduplicate by message ID for census; deduplicate identical content before counting broadcast events. A broadcast to 60 recipients is one occupancy report, not 60 runs. Offset pagination is moving, so overlapping exports were required; matching boundaries reduce gaps but cannot prove every unreported attempt was captured.
- No hub queries, direct SQL, live mutation, daemon restart, or test run was performed for this research. Local Git inspection and this one artifact are the only filesystem work.
- ORCH's 17:24 CDT ruling, relayed by LM1: **exclude session 16861's data by any route**; reconstruct #24622 Native WS only from coordinator/RM messages. No session-16861 data was fetched. No close attempt for that task is established by the permitted records at cutoff; its direct intervals remain unobservable. The restriction prevents filling those gaps from the owner transcript.
- `get_task(brief=false)` supplies receipt tuples, not receipt UUIDs. Receipt citations below use the exact **task UUID + kind + full source SHA + timestamp** composite locator. Message and agent-run UUIDs are given where available. Source facts and retrieval-result IDs allow rechecking without inventing receipt IDs.

### What an interval means

READY→GO needs an accepted, same-phase READY and its single-use admission. GO→PASS ends at a durable PASS report; test duration alone is not queue time. A first-ever task CANDIDATE stamp cannot identify the latest rework candidate, so it is not used as final-candidate review latency. Independent approval→landing receipt includes approval/merge/protocol/recording delay; it is not LAND→receipt unless the LAND event itself is timestamped. Landing→activation is a receipt-based proxy: it includes retesting, batching, and receipt delay, and is not pure downtime. Close-run time is `completed_at-started_at`; GO→run-start also includes the caller and admission handoff and cannot be labeled internal FIFO queue time.

Intervals are clipped to today's window. Task-minutes add parallel waits across tasks. They are not wall-clock minutes, and stages can overlap. Missing endpoints are **U (unobservable)**, never zero. N/A is reserved for organizational parents or a proven inapplicable stage. Right-censored holds are excluded from completed totals.

## Verified closure and attempt cohort

This table is the union of 15 observed closed tasks and eight further leaves with explicit attempt evidence. It is a **verified lower-bound census**, not a claim that a missing attempt did not happen. In particular, #24595 Caller machine scope demonstrates why an updated-task inventory alone is insufficient.

| Task | Outcome at cutoff / attempt evidence | Closed UTC | Task UUID (T citation) |
| --- | --- | --- | --- |
| #24582 Ledger prune fixture | Closed; reviewer run dac64fdd-9651-4e23-b3c0-b8a1cf238a0b; M1 | 05:14:23 | `9af2f8aa-f6ed-4453-a352-87ba04fdb584` |
| #24642 Gate 10 integration credit | Closed after extra 13-file / 957 PASS coverage; M2 | 06:41:32 | `4492d7ca-d1a4-465d-b29e-60d1d52e8dc6` |
| #21588 Voice route epic | Organizational closure; leaf pipeline N/A | 07:09:59 | `2b1c4ba2-f8ab-4bf6-9cb1-04d531071053` |
| #24647 Daemon recovery | Closed after Gate 7, added Gate 10 targets, invalid rollback review; M3 | 08:42:15 | `52e4707e-0986-4cae-8d2f-995fda8473ce` |
| #24644 Relative rule-count plan | Closed; first close blocked Gate 6 summary; M4 | 10:22:27 | `00333a20-a015-4ba0-9d59-3979421afde7` |
| #24520 checkout.read contract | Closed; first review timed out without verdict, second valid; M5 | 15:14:47 | `d378b448-7448-45f2-9887-c04c919784f9` |
| #24350 MCP dialer | Closed; 13/13 criteria; M6 | 15:27:42 | `b22eb0e3-c313-4825-966f-ea76c18bd743` |
| #24361 Node hook ingress epic | Organizational closure with last child; leaf pipeline N/A | 17:17:16 | `8780e671-2945-4c6a-9812-dd15684f599d` |
| #24369 Node spool drain | Closed; 10/10 criteria; M7 | 17:17:16 | `05a4a95b-0307-4fa6-8552-08a2a265bee2` |
| #24381 Detection manifest | Closed; preview Gate 8 scope / Gate 10 coverage repair; M8 | 18:00:22 | `ec81ff78-4b27-4a75-aeeb-4a124d745060` |
| #24715 Ledger cleanup | Closed; real-close GO M9 | 19:37:54 | `243b19d3-76d3-4956-b1a1-920ea64cc4c2` |
| #24370 Durable node state | Closed; close GO M10 | 21:32:46 | `637362e7-60f5-4b09-970d-a4e3feeb7ffa` |
| #24645 Review-gate correction | Closed after invalid first review and corrective candidate; M11 | 21:36:37 | `38391e89-c2fd-4631-9a27-70a46cdbc4ba` |
| #20438 Temp-root docs | Closed; R5 and M12 | 22:05:57 | `7779614a-6819-4379-bced-ddc557d42e1d` |
| #24569 Rust tmux | Closed after deterministic Gate 7 / Gate 10 refusal; R2 and M13 | 21:55:55 | `725d5649-0fb5-4f5a-94f7-0745215c3dbe` |
| #24423 Gate 12 tightening | Open; deterministic refusal / invalid review / diagnostic preview outcome incompletely recovered; M14 | — | `c1e2a932-7177-4434-a9b2-d2cf68301fde` |
| #24605 Native variable authority | Open; preview Gate 7 ancestor linkage refused; M15 | — | `eb11d850-60dc-4964-9665-6d9e6409fbd4` |
| #24537 Gate 12 stub RED | Open; Gate 10 refusal, 88.7 s; M16 | — | `b58bd8a0-df8a-4dc2-b3fa-ba4bddd62293` |
| #24631 Gate 12 transcript RED | Open; Gate 10 preview refusal, then invalid criterion 4 review; R4 and M17 | — | `9e91f79c-1b1f-49e9-b8e3-012e143a3a17` |
| #24270 Node routes | Open; close preview recovery, landed retest foreign failures; M18 | — | `95a23095-21a2-4591-bcea-35576b8f9d12` |
| #24650 Seat recovery | Open; invalid close criterion 3; M19 | — | `a3b04fba-da59-4df2-ab92-ba742ccaa008` |
| #24558 gcode Git cost | Open; medium argv-size finding; R3 | — | `b1fc569d-03ba-453d-a010-90e7a1363120` |
| #24595 Caller machine scope | Open; preview Gate 12 missing REDs; task updated_at predates day despite today's attempt; M20 | — | `3683b99d-dd47-419d-a837-b0bd830232d5` |

## Per-task intervals

All eight requested stages are represented below. Receipt stages have complete endpoint rows in the next section. The validation table separates an observed run duration from an unavailable READY/GO/PASS pair.

| Task | READY→RM GO | GO→PASS | Gate 10 reruns: lower-bound evidence | CANDIDATE→reviewer LAND | LAND→land_commit receipt | Landed retest wait | Activation hold | Close queue / run |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| #24582 Ledger prune fixture | U | U | ≥1 expanded 10-file set: 210 PASS, 743.10 s (M1) | U | U | U | A cycles | queue U; terminal 05:14:37 (M1) |
| #24642 Gate 10 integration credit | U | U | ≥1 extra 13-file set: 957 PASS,137.90 s after 681 PASS/128.57 s (M2) | U | U | U | A cycles | queue/run U; terminal valid M2 |
| #21588 Voice route epic | N/A organizational parent | N/A organizational parent | N/A organizational parent | N/A organizational parent | N/A organizational parent | N/A organizational parent | N/A organizational parent | N/A organizational parent |
| #24647 Daemon recovery | U | U | ≥2: fresh18/811 PASS,107.64 s; later2 files/175 PASS (M3) | U | U | U | A cycles | multiple previews; invalid rollback finding then valid (M3) |
| #24644 Relative rule-count plan | U | N/A docs | N/A docs | U | U | N/A docs | N/A docs | Gate6 summary repair; queue/run U (M4) |
| #24520 checkout.read contract | 5.90 close queue (Q1) | U; covering138 PASS/9.53 s M5 | ≥1 covering7; exact total U | U | U | U | A cycles | GO→start1.11; run10.64; prior timeout U (R7) |
| #24350 MCP dialer | U | U | ≥1 covering9 (M6); exact total U | U | U | U | A cycles | GO→start0.73; run7.66 (R6) |
| #24361 Node hook ingress epic | N/A organizational parent | N/A organizational parent | N/A organizational parent | N/A organizational parent | N/A organizational parent | N/A organizational parent | N/A organizational parent | N/A organizational parent |
| #24369 Node spool drain | U | U | U | U | U | U | A cycles | queue/run U; valid terminal12:17:37 CDT (M7) |
| #24381 Detection manifest | U | U | ≥1 extra5 files after scope/coverage preview (M8) | U | U | U | A cycles | queue/run U; valid M8 |
| #24715 Ledger cleanup | U | U | U | U | U | receipt says retest not required | A cycles | queue/run U; close GO M9 |
| #24370 Durable node state | U | U; installers93 PASS/4.76 s M10 | ≥1 installers4 (M10) | U | U | U | A cycles | queue/run U; valid21:32:46 T |
| #24645 Review-gate correction | 8.30 initial close queue (Q2) | landed GO→DONE14.94 (Q3); supplementary timing U | ≥1 supplementary4:87 PASS/88.98 s after310 PASS/756.62 s (M11) | U | U | accepted retest READY→GO3.28 (Q3) | A cycles | GO→start0.87; run12.62 (R1); earlier invalid M11 |
| #20438 Temp-root docs | U | N/A docs | N/A docs | U | U | U | N/A docs | queue U; run4.84 (R5) |
| #24569 Rust tmux | U | U; installers93 PASS qualified exit M13 | ≥1 installer set after Gate7/Gate10 blocker (M13) | U | U | U | A cycles | GO→start0.85; run11.45 (R2) |
| #24423 Gate 12 tightening | U | U; 1036 PASS/264.58 s early retest | ≥2 scope expansions; missing1 file then larger related set; exact total U (M14) | U | U | U | A cycles | preview timing U; queue/run U; remains open |
| #24605 Native variable authority | U | U | U; Gate7 ancestor linkage blocks preview (M15) | U | U | U | A cycles | no reviewer launched by that refusal; queue/run U |
| #24537 Gate 12 stub RED | U | U | ≥1 expanded landed12, then Gate10 refusal: stale types/tsc/integration credit (M16) | U | U | U | A cycles | deterministic refusal88.7 s (M16); queue/run U |
| #24631 Gate 12 transcript RED | U | U; supplementary339 PASS/80.89 s (M17) | ≥1 fresh8 rerun after differing-tree preview refusal (M17) | U | U | U | A cycles | GO→start1.17; run15.61 invalid (R4) |
| #24270 Node routes | U | U | ≥1 landed26:396 PASS/5 FAIL,353.30 s; foreign gcode failures (M18) | U | U | U | A cycles | preview recovery; queue/run U |
| #24650 Seat recovery | U | U; landed2168 PASS/1975.73 s M19 | U; fixture failures and rework also present | U | U | U | A cycles; later test-only landing N/A | invalid criterion3; exact queue/run U (M19) |
| #24558 gcode Git cost | U | U; close135 PASS qualified M21 | ≥1 eight-file set before preview (M21) | U | U | landing receipt says retest not required | A cycles | GO→start0.95; run11.91 invalid (R3) |
| #24595 Caller machine scope | U | U | U; Gate10 pass in preview M20 | U | U | U | no completed in-window A cycle | Gate12 six missing REDs; no reviewer established (M20) |

### Receipt-based stage totals and endpoints

A = landing receipt→next activation receipt, only when no intervening landing occurs. L = exact-source independent approval→landing receipt. Both endpoints are shown; dates before today are carry-in and clipped at 05:00 UTC. Approval is a recorded LAND verdict, not necessarily the time of the earlier protocol event. All composite receipt locators use the T UUID above.

| Stage | Task | Source SHA / activation SHA | Start UTC | End UTC | In-window minutes |
| --- | --- | --- | --- | --- | --- |
| A | #24642 Gate 10 integration credit | `560583d732e6087c81877acfb4060b9ada33f88e` / `6f4c051645345506bb69e2cd0b937d4ee5dde431` | 2026-10-18T05:22:43.842995+00:00 | 2026-10-18T05:57:09.795752+00:00 | 34.43 |
| A | #24647 Daemon recovery | `45219c11e2bdb68b07dec670ce85265dca5234f9` / `93becb485d68a0d5fe2a4add0391ccd4596b8bfa` | 2026-10-18T05:50:27.437218+00:00 | 2026-10-18T05:57:13.520778+00:00 | 6.77 |
| A | #24647 Daemon recovery | `0f9461044f316bcf35e12eff45f7920f932f8e3c` / `0f9461044f316bcf35e12eff45f7920f932f8e3c` | 2026-10-18T07:39:50.130026+00:00 | 2026-10-18T07:58:57.220240+00:00 | 19.12 |
| A | #24520 checkout.read contract | `bc770f357217d740320a9013ec5e837bce3cd7a8` / `bc770f357217d740320a9013ec5e837bce3cd7a8` | 2026-10-18T07:09:01.322578+00:00 | 2026-10-18T07:58:59.482381+00:00 | 49.97 |
| A | #24350 MCP dialer | `709afd53c4e410c34b62ebab5ea8756557c1efbf` / `709afd53c4e410c34b62ebab5ea8756557c1efbf` | 2026-10-18T06:45:35.710570+00:00 | 2026-10-18T07:59:01.162309+00:00 | 73.42 |
| A | #24369 Node spool drain | `98379a262647a913f48e14f8e1ff67d7de90b644` / `07b7be6c3fefdee0745a033b421ba05631d4e15f` | 2026-10-18T16:10:50.394831+00:00 | 2026-10-18T16:46:00.329344+00:00 | 35.17 |
| A | #24381 Detection manifest | `073fc1dbb175b502828d8b410d4b0c4a7d58ecf7` / `073fc1dbb175b502828d8b410d4b0c4a7d58ecf7` | 2026-10-18T05:59:05.012014+00:00 | 2026-10-18T07:59:03.763836+00:00 | 119.98 |
| A | #24381 Detection manifest | `df07a73783e7caa4797ae7216e87570d4f7bbb33` / `b87113860732f435076985dce0ee1c83e696df5e` | 2026-10-18T08:44:08.135472+00:00 | 2026-10-18T16:46:07.531051+00:00 | 481.99 |
| A | #24715 Ledger cleanup | `92580f8209619a9b66972af712a9ddde17922be9` / `92580f8209619a9b66972af712a9ddde17922be9` | 2026-10-18T18:42:26.827753+00:00 | 2026-10-18T19:15:42.767040+00:00 | 33.27 |
| A | #24370 Durable node state | `8717e525ac96694097bcd71a8b247a50410da57a` / `6eabe8b53c1bb49db16743a1810f7024a0295583` | 2026-10-18T19:46:20.324620+00:00 | 2026-10-18T21:06:20.017657+00:00 | 79.99 |
| A | #24645 Review-gate correction | `afca0817c9627ecab3902ca6b6eda6dcbcd1a4e4` / `8913a6a8eb59cf67581b090b2bb49a7c3eb1bf36` | 2026-10-18T17:59:02.788265+00:00 | 2026-10-18T19:15:38.410448+00:00 | 76.59 |
| A | #24645 Review-gate correction | `8d78e102b5703f02c71c025a2eb5829ca9228780` / `6bc790a7cbad488911a13aae6d4290e6697e568c` | 2026-10-18T20:22:21.532796+00:00 | 2026-10-18T21:06:16.362354+00:00 | 43.91 |
| A | #24569 Rust tmux | `5d39f5751aeb75869b42b017dc7cdd7633741a18` / `c24721fe9ddf6f1720e5333f7758c350dfe4903f` | 2026-10-18T20:24:10.124097+00:00 | 2026-10-18T21:06:14.304846+00:00 | 42.07 |
| A | #24423 Gate 12 tightening | `03d8646e0e8a69b4e5cdbdc8f1dc532276a81b99` / `03d8646e0e8a69b4e5cdbdc8f1dc532276a81b99` | 2026-10-18T06:32:05.117429+00:00 | 2026-10-18T07:59:07.601875+00:00 | 87.04 |
| A | #24423 Gate 12 tightening | `7755e221e771a1ce4e2e865e949703d42498b159` / `2515553f318d43b86989957f13476cdcaef4e869` | 2026-10-18T17:47:29.146872+00:00 | 2026-10-18T19:15:34.808751+00:00 | 88.09 |
| A | #24605 Native variable authority | `1c64eccc8b33a25f070417557a0dc1e7cccbb566` / `1c64eccc8b33a25f070417557a0dc1e7cccbb566` | 2026-10-18T07:39:30.955048+00:00 | 2026-10-18T07:58:55.109611+00:00 | 19.40 |
| A | #24605 Native variable authority | `838ea9ab82c650b6cd39cca07e83134b2beb1dc8` / `ae3397e99c7710f49ed40f2523a4ba351ffc0a80` | 2026-10-18T18:51:32.814082+00:00 | 2026-10-18T19:15:44.998277+00:00 | 24.20 |
| A | #24537 Gate 12 stub RED | `09399cff0c0dcd441b1c09ed2a147cb51b51488f` / `207d2e102dfca1c79fc691279502c95dd1aebcd1` | 2026-10-18T17:29:31.852727+00:00 | 2026-10-18T19:15:31.853556+00:00 | 106.00 |
| A | #24631 Gate 12 transcript RED | `09d869ea7a456131fee019edfcd33f6069823fbd` / `6ff393368a7483a63c1ee6046ab5eaba7b093f1a` | 2026-10-18T16:35:10.227241+00:00 | 2026-10-18T16:46:13.273982+00:00 | 11.05 |
| A | #24270 Node routes | `f0a600e55c044e354181b8e699152c157319bfd9` / `f0a600e55c044e354181b8e699152c157319bfd9` | 2026-10-17T15:23:01.526808+00:00 | 2026-10-18T17:26:08.880817+00:00 | 746.15 |
| A | #24650 Seat recovery | `9ee1ed291e4953372a18a67e5143f277213921e5` / `d60da17bde6727ef49f96b5a86e715678cbf76dc` | 2026-10-18T05:39:20.336097+00:00 | 2026-10-18T05:57:10.732004+00:00 | 17.84 |
| A | #24650 Seat recovery | `ce2f68ccf1ad01bcfd44090a9855a6c8a537c37e` / `4bf09396887c585f0770e44908602f62b2bba1fa` | 2026-10-18T08:43:23.484291+00:00 | 2026-10-18T16:46:10.993813+00:00 | 482.79 |
| A | #24558 gcode Git cost | `4f229f6d5626b861af3293c5aa0270201aba6674` / `4f229f6d5626b861af3293c5aa0270201aba6674` | 2026-10-18T20:49:02.435941+00:00 | 2026-10-18T21:06:12.175029+00:00 | 17.16 |
| L | #24642 Gate 10 integration credit | `560583d732e6087c81877acfb4060b9ada33f88e` | 2026-10-18T05:11:25.873922+00:00 | 2026-10-18T05:22:43.842995+00:00 | 11.30 |
| L | #24647 Daemon recovery | `45219c11e2bdb68b07dec670ce85265dca5234f9` | 2026-10-18T05:45:35.608447+00:00 | 2026-10-18T05:50:27.437218+00:00 | 4.86 |
| L | #24647 Daemon recovery | `0a1f9c408e5cfb6554761bc09123601af6298926` | 2026-10-18T06:17:01.775646+00:00 | 2026-10-18T06:17:21.983070+00:00 | 0.34 |
| L | #24647 Daemon recovery | `0f9461044f316bcf35e12eff45f7920f932f8e3c` | 2026-10-18T07:25:07.758997+00:00 | 2026-10-18T07:39:50.130026+00:00 | 14.71 |
| L | #24520 checkout.read contract | `bc770f357217d740320a9013ec5e837bce3cd7a8` | 2026-10-18T07:05:23.987225+00:00 | 2026-10-18T07:09:01.322578+00:00 | 3.62 |
| L | #24350 MCP dialer | `709afd53c4e410c34b62ebab5ea8756557c1efbf` | 2026-10-18T06:42:05.912572+00:00 | 2026-10-18T06:45:35.710570+00:00 | 3.50 |
| L | #24369 Node spool drain | `98379a262647a913f48e14f8e1ff67d7de90b644` | 2026-10-18T16:06:15.014372+00:00 | 2026-10-18T16:10:50.394831+00:00 | 4.59 |
| L | #24381 Detection manifest | `073fc1dbb175b502828d8b410d4b0c4a7d58ecf7` | 2026-10-18T05:48:36.579772+00:00 | 2026-10-18T05:59:05.012014+00:00 | 10.47 |
| L | #24381 Detection manifest | `df07a73783e7caa4797ae7216e87570d4f7bbb33` | 2026-10-18T08:37:36.155083+00:00 | 2026-10-18T08:44:08.135472+00:00 | 6.53 |
| L | #24715 Ledger cleanup | `92580f8209619a9b66972af712a9ddde17922be9` | 2026-10-18T18:37:30.402973+00:00 | 2026-10-18T18:42:26.827753+00:00 | 4.94 |
| L | #24370 Durable node state | `8717e525ac96694097bcd71a8b247a50410da57a` | 2026-10-18T19:26:35.480132+00:00 | 2026-10-18T19:46:20.324620+00:00 | 19.75 |
| L | #24645 Review-gate correction | `afca0817c9627ecab3902ca6b6eda6dcbcd1a4e4` | 2026-10-18T17:53:32.249651+00:00 | 2026-10-18T17:59:02.788265+00:00 | 5.51 |
| L | #24645 Review-gate correction | `8d78e102b5703f02c71c025a2eb5829ca9228780` | 2026-10-18T20:17:48.327094+00:00 | 2026-10-18T20:22:21.532796+00:00 | 4.55 |
| L | #20438 Temp-root docs | `075ff65f46d0d991b0f3d2fdd098bf8817f275fe` | 2026-10-18T21:38:11.801579+00:00 | 2026-10-18T21:38:50.252002+00:00 | 0.64 |
| L | #24569 Rust tmux | `5d39f5751aeb75869b42b017dc7cdd7633741a18` | 2026-10-18T20:19:35.581257+00:00 | 2026-10-18T20:24:10.124097+00:00 | 4.58 |
| L | #24423 Gate 12 tightening | `03d8646e0e8a69b4e5cdbdc8f1dc532276a81b99` | 2026-10-18T06:31:20.088112+00:00 | 2026-10-18T06:32:05.117429+00:00 | 0.75 |
| L | #24423 Gate 12 tightening | `7755e221e771a1ce4e2e865e949703d42498b159` | 2026-10-18T17:45:43.268671+00:00 | 2026-10-18T17:47:29.146872+00:00 | 1.76 |
| L | #24605 Native variable authority | `1c64eccc8b33a25f070417557a0dc1e7cccbb566` | 2026-10-18T07:38:22.246622+00:00 | 2026-10-18T07:39:30.955048+00:00 | 1.15 |
| L | #24605 Native variable authority | `838ea9ab82c650b6cd39cca07e83134b2beb1dc8` | 2026-10-18T18:48:10.478249+00:00 | 2026-10-18T18:51:32.814082+00:00 | 3.37 |
| L | #24537 Gate 12 stub RED | `09399cff0c0dcd441b1c09ed2a147cb51b51488f` | 2026-10-18T17:17:34.624684+00:00 | 2026-10-18T17:29:31.852727+00:00 | 11.95 |
| L | #24631 Gate 12 transcript RED | `09d869ea7a456131fee019edfcd33f6069823fbd` | 2026-10-18T16:32:16.786034+00:00 | 2026-10-18T16:35:10.227241+00:00 | 2.89 |
| L | #24631 Gate 12 transcript RED | `5810ad3c13142efcc095e2df74fc970cdf288c35` | 2026-10-18T18:06:58.284326+00:00 | 2026-10-18T19:27:42.968841+00:00 | 80.74 |
| L | #24650 Seat recovery | `9ee1ed291e4953372a18a67e5143f277213921e5` | 2026-10-18T05:37:42.345093+00:00 | 2026-10-18T05:39:20.336097+00:00 | 1.63 |
| L | #24650 Seat recovery | `ce2f68ccf1ad01bcfd44090a9855a6c8a537c37e` | 2026-10-18T08:39:36.153890+00:00 | 2026-10-18T08:43:23.484291+00:00 | 3.79 |
| L | #24650 Seat recovery | `7084a4aa4f8eabfe169406f97e9ab3d9ece588ad` | 2026-10-18T16:29:07.772601+00:00 | 2026-10-18T19:27:14.918904+00:00 | 178.12 |
| L | #24558 gcode Git cost | `4f229f6d5626b861af3293c5aa0270201aba6674` | 2026-10-18T20:41:26.331840+00:00 | 2026-10-18T20:49:02.435941+00:00 | 7.60 |

## Ranked measured durations, and what remains unrankable

| Rank among measured totals | Stage | Total minutes | Coverage / interpretation |
| --- | --- | --- | --- |
| 1 | Landing→activation receipt | **2696.42** | 23 completed A cycles; includes carry-in for Node routes; retest and activation are not separated |
| 2 | Independent approval→landing receipt | **393.65** | 26 completed L cycles; includes long rework/receipt gaps, not just protocol overhead |
| 3 | Close-review execution | **74.74** | Seven complete R runs only; excludes earlier invalid/timeout runs not fully recovered |
| — | RM admission queue | **14.20** | Only Q1+Q2, two verified close READY→GO pairs; not comparable to broader totals |
| — | Repeated validation / landed retesting | **U total** | Named runs measured below; cannot assign all to avoidable repetition without fingerprints |
| — | Final CANDIDATE→LAND review queue | **U total** | Mutable task-level first CANDIDATE stamp does not bind rework SHA |
| — | Internal close-review FIFO queue | **U total** | No queued/launching/promoted timestamps exposed by recovered runs |
| — | Exclusive avoidable time lost | **U** | Requires complete attempt log plus mutually exclusive state intervals and counterfactual policy |

The first two mechanisms dominate the available timestamp totals. This does **not** establish a fleet-wide ordering of wasted time: missing stages can change the order. Do not add the rows into one elapsed-time number. #24270 Node routes contributes 746.15 in-window A minutes; #24650 Seat recovery contributes 482.79; #24381 Detection manifest contributes 481.99 in its second A cycle. Their sum is concurrent task wait, not host downtime.

### Validation cost examples

- #24645 Review-gate correction: source final310 PASS/638.35 s, landed310 PASS/756.62 s, then supplementary87 PASS/88.98 s. The last scope addition cost 1.48 test-minutes plus lifecycle recovery/admission. The two 310-test runs account for 23.25 minutes of execution, but one verifies a changed integration binding; the entire cost cannot be called redundant without input comparison. Evidence M11, Q3, and messages `9020faa3-e741-4834-908f-2134148b6930`, `24fd49c8-c622-4f21-8628-3a9364cbb9d3`.
- #24642 Gate 10 integration credit: 681 PASS/128.57 s followed by a missing13-file957 PASS/137.90 s; 4.44 minutes of test execution across the two reported runs. M2 and memory `6a0a58a0-d24f-5da5-be70-eaf32ac7ccb8` identify scope expansion after merge.
- #24631 Gate 12 transcript RED: final fresh339 PASS/80.89 s after a differing-tree/integration-credit refusal. M17. The later invalid close exposes a real criterion 4 gap, not a reason to waive review.
- #24650 Seat recovery: landed2168 PASS/1975.73 s (32.93 minutes), then an invalid close because resume plus developer-step-enforced close was not actually covered. M19. A broad pass count did not replace the missing acceptance proof.
- #24270 Node routes: landed26-file run396 PASS/5 FAIL/353.30 s. All five failures confined to the active gcode owner, and the handoff records the exact paths and command. M18. A total rerun should not be the default response when unchanged owned paths already passed and strict confinement credit applies.

## Close-review execution and queue bounds

R citations are `get_agent_result(run_id=...)` records. Terminal status success describes delivery; R3 and R4 delivered INVALID verdicts and did not close their tasks. Completion includes finalization/drain, so run time is not pure model inference.

| Ref | Task | Run UUID | Started UTC | Completed UTC | Run minutes | RM GO→start minutes |
| --- | --- | --- | --- | --- | --- | --- |
| R1 | #24645 Review-gate correction | `36be5525-11b0-48af-8add-f4385819883d` | 2026-10-18T21:24:20.165996+00:00 | 2026-10-18T21:36:57.072313+00:00 | 12.62 | 0.87; `7d814c3b-0c61-4e6e-b420-449f59cc0534` |
| R2 | #24569 Rust tmux | `592aa1a1-fa31-4785-b774-8b935fc9f8e0` | 2026-10-18T21:44:46.416626+00:00 | 2026-10-18T21:56:13.358413+00:00 | 11.45 | 0.85; `477606ef-d56e-4b49-9f5b-5d1781ed12ba` |
| R3 | #24558 gcode Git cost | `258523e2-e953-48a5-868c-5652e1f72684` | 2026-10-18T21:50:40.078557+00:00 | 2026-10-18T22:02:34.901785+00:00 | 11.91 | 0.95; `ef834409-c180-4ff0-a932-f8bcc63061e2` |
| R4 | #24631 Gate 12 transcript RED | `a8f253cb-f10d-425d-bb79-d427fd52e692` | 2026-10-18T21:51:50.683514+00:00 | 2026-10-18T22:07:27.569768+00:00 | 15.61 | 1.17; `de7bda95-a5ca-4555-8d79-311194d4977d` |
| R5 | #20438 Temp-root docs | `0edbc8c7-5f7a-4e22-ba45-cbe06d0012a4` | 2026-10-18T22:01:18.615150+00:00 | 2026-10-18T22:06:09.116092+00:00 | 4.84 | U |
| R6 | #24350 MCP dialer | `7b64ac82-7d27-4bf0-b45c-2312217514ad` | 2026-10-18T15:20:20.344723+00:00 | 2026-10-18T15:27:59.966971+00:00 | 7.66 | 0.73; `6e4a6d63-add5-41ac-96b0-73d881a75636` |
| R7 | #24520 checkout.read contract | `15a075dc-b5e1-4794-9242-86594f2a5b15` | 2026-10-18T15:04:25.564772+00:00 | 2026-10-18T15:15:04.002084+00:00 | 10.64 | 1.11; `1262ad83-4925-4608-8350-fcd631bb1e4e` |

R3: medium unbounded `git ls-tree` argv; all three stated criteria otherwise satisfied. R4: criterion 4 new-main-transcript-path claim unsupported for Claude/Codex; same-path external-ID archive works. These reviews found substantive defects. R7 recorded 225 sandbox violations; most other recovered runs recorded 10–12. Counts do not measure lost time or establish that every denial blocked useful work; trace-level attribution would be needed.

### Three directly paired queue/retest intervals

| Ref | Task / phase | Start message UUID and UTC | End message UUID and UTC | Minutes |
| --- | --- | --- | --- | --- |
| Q1 | #24520 checkout.read close READY/release→GO | `e2203689-d2c2-4545-88e9-8c3896de1030`,08:37:49.522870 | `00c541f6-56bd-416e-b95f-a6264ceee8dc`,08:43:43.445908 | 5.90 |
| Q2 | #24645 Review-gate initial CLOSE_READY→GO | `bfa0b2c2-9216-424c-bd35-5cc19598dfe3`,19:18:47.654491+00:00 | `bd380a3b-a6ac-4ac4-9377-da926a3f0272`,19:27:05.754293 | 8.30 |
| Q3a | #24645 Review-gate landed retest READY accepted→GO | `693397ca-c421-4dcd-8e32-b49fec8bd042`,20:29:42.092864+00:00 | `ac692c16-24b3-408b-aa0f-8fb8366464cf`,20:32:58.967992 | 3.28 |
| Q3b | #24645 Review-gate landed retest GO→RM DONE report | `ac692c16-24b3-408b-aa0f-8fb8366464cf` | `24fd49c8-c622-4f21-8628-3a9364cbb9d3`,20:47:55.195179 | 14.94 |

GO→DONE includes dispatch/reporting plus run time; the direct run duration is 756.62 s. A second close GO after a timeout must not be paired with an earlier READY as if the entire intervening review were idle queueing.

## Slot occupancy over the day

Exact integrated close-slot-minutes and hub-slot-minutes are **unobservable** from sampled prose alone. Admission, unused-GO revocation, START, terminal verdict, and drain are different events. Reports also mix reserved and running slots, and one close consumes hub capacity under the earlier combined interpretation. The table below reports durable observations rather than interpolating a whole-day utilization curve.

| UTC / CDT | Reported hub / close / heavy | Evidence |
| --- | --- | --- |
| 05:15 /00:15 | hub1/3,close0/2 after ledger-prune release | `132fa34d-c809-4fdd-8a19-a132478ec220` |
| 06:44 /01:44 | hub3/3,close1/2,heavy1/1 | `5002da6f-fc9d-4c28-9f8b-f13d4dd152b2` |
| 07:48 /02:48 | hub0/3,close0/2,heavy0/1; QUIET | `a836d257-956c-4390-954a-97523faeb6de` |
| 08:21 /03:21 | two close places reserved; hub3/3 reserved incl pending recovery | `079c6c30-e175-48ce-b099-84dfc935f096` |
| 15:04 /10:04 | hub3/3; two close admissions + Native authority RED; heavy1/1 | `40bddcee-0065-4423-a1e2-c72da6129282` |
| 16:36 /11:36 | hub0/3,close0/2,heavy0/1; QUIET | `1789e860-7bc9-4789-9d05-9b15ad5d4243` |
| 17:03 /12:03 | hub3/3,close1/2,heavy1/1 | `d794f225-c7c0-4fac-bccb-27bf0b480128` |
| 17:26 /12:26 | hub0/3,close0/2,heavy0/3 | `aff386ea-45e1-45a9-9f14-9fd9c17811ff` |
| 19:01 /14:01 | hub0/3,close0/2,heavy0/3; QUIET | `3795f7ef-0f25-4968-9d6e-c15536e121ce` |
| 20:54 /15:54 | all admitted work drained; QUIET | `72ff416b-1123-4df4-a903-59447741d304` |
| 21:50 /16:50 | close3/3; heavy2/3; interpretation question about separate close/hub caps | `133664f5-7d80-46d6-aad4-4ea267d1fc57` |
| 22:08 report /17:08 | Temp-root docs reviewer terminal, place released; Node routes next | `1e5fbdc6-5d6a-46bb-a4cf-2c134ad00cb7` |

Capacity policy changed during the day. RM's source summary `c2625ec9-de50-4122-aaa3-8fe092a22d99` cites memory `b21324f3` for the 12:05 CDT heavy-cap-three/two-hub-node reservation and ORCH message `28c457a0-db33-4afe-811e-e90a0b5ec26b` for 16:49 CDT close-cap-three. The aggregate-versus-separate hub/close interpretation was still explicitly awaiting clarification. Do not retroactively apply the later capacity to earlier observations or call a reserved but unused place active work.

## Concrete improvements

| Mechanism / priority | Fix and measurable success condition | Classification | Existing work |
| --- | --- | --- | --- |
| Activation wait, highest measured total | Publish a bounded activation cadence and cutover budget, with class-specific admission. Keep tests/docs class-none paths off restart holds when independent approval suffices. Measure landing-ready→activation separately from landed test completion. Choose cadence from observed queue and cutover cost. | **Policy** for class handling; **operator decision** for cadence/resource tradeoff | #24605 Native variable authority; #24650 Seat recovery; #24652 SRT monitor restart |
| Receipt and protocol latency, second measured total | Record exact candidate/SHA LAND event and receipt UUID together; return actionable missing-approval/line-parse errors before waiting for a later close. Bind releases to the candidate fingerprint, invalidate only on relevant change, and show the missing owner action in one card. Measure LAND-event→receipt, not first task-candidate→receipt. | **Code** for attestation/readback; **policy** for one canonical line and exact invalidation | #24605 Native variable authority |
| Repeated validation and scope discovery | Freeze the required changed/related target manifest before final validation; expose Gate10 preflight without a close attempt. Reuse by changed/import/conftest/environment fingerprints; record precise invalidation cause. Run only newly uncovered targets when the unchanged set remains valid; never reuse across changed inputs by assertion alone. | **Code** | #24538 Bounded runs and Gate 10 reuse; #24642 Gate 10 integration credit |
| RED attribution / acceptance mismatch | Surface attributable RED evidence gaps before landing and distinguish same-path archive coverage from new-main-transcript discovery. Emit which node/criterion lacks proof, not just a broad requirement to rerun. | **Code**; **policy** for precise acceptance summaries | #24631 Gate 12 transcript RED; #24423 Gate 12 tightening; #24537 Gate 12 stub RED |
| Recovery loop after deterministic refusal | Preserve claim, evidence window, candidate and allowed repair commands through supported submit→implement recovery. A close refusal should return a supported route in the same response; no coordinator-only multi-message bounce for a scoped test repair. Measure refusal→executable recovery. | **Code** | #24605 Native variable authority; #24650 Seat recovery |
| Restart/terminal disruption | Prove monitor ownership/preservation and seat/claim recovery using isolated restart tests before operational cutover. Drain reviewers event-wise before stop, acknowledge handoffs during HOLD, and verify activation identity before releasing. | **Code** for recovery; **policy** for QUIET/drain | #24652 SRT monitor restart; #24650 Seat recovery |
| Close-review duration | Supply a bounded criterion-to-evidence matrix, exact net patch, and reusable validated fingerprint. Preserve substantive independent review: R3/R4 demonstrate defects found after deterministic PASS. Increasing concurrency without host measurements risks longer runs and more transient failures. | **Code** for evidence packet; **operator decision** for any concurrency change | #24538 Bounded validation records/reuse |
| Slot accounting ambiguity | One durable lease ledger per resource with requested/admitted/started/drained/revoked timestamps; define whether close places also count against hub capacity. Publish capped backlog and priority in one view. No repeated broadcast fanout needed for unchanged state. | **Code**, **policy**; **operator decision** for cap model | 20-awaiting-close rule in ORCH message `1db06d09-713a-47da-97bf-4bbfc3633fcf` |
| Missing measurement surface | Add a read-only date/task-filtered attempt export containing preview outcomes, gate timing/fingerprint, queued/promoted/started/completed timestamps, receipt UUIDs, resource leases and parent/candidate/run IDs. Include failed deterministic attempts that do not update the task row. Stable snapshot/cursor pagination prevents history shifts during collection. | **Code**; ORCH must name an existing surface or authorize a follow-up task | Finding owned by this research; request `bdc029ab-bd0c-4ea3-8c6b-c4549f125ea0` |

No runtime policy or code was changed by this report. These are proposals requiring ordinary owned implementation and validation, not permission to waive gates or launch more workers.

## Evidence index

- **M1**: message `132fa34d-c809-4fdd-8a19-a132478ec220`, 2026-10-18T05:15:29.858800+00:00. Ledger prune closed VALID; terminal 05:14:37 UTC, 210-pass expanded validation, drained.
- **M2**: message `e2fca823-efa7-4427-a8cd-5c29cd0ca62f`, 2026-10-18T06:43:17.771811+00:00. Gate 10 integration credit closed VALID after the additional 13-file / 957-pass run.
- **M3**: message `558cbe49-cafb-4d38-83c4-230087345c5b`, 2026-10-18T08:16:40.172760+00:00. Daemon recovery preview requires two more Gate 10 files; supported lifecycle recovery requested.
- **M4**: message `2d3c24b9-6ab2-449d-98d4-c459a014ff11`, 2026-10-18T08:35:27.066220+00:00. Relative rule-count close first blocked on Gate 6; fresh close supplied the missing changes summary.
- **M5**: message `52216136-d896-4d6c-bde2-f0a557c3f6af`, 2026-10-18T08:26:41.478034+00:00. checkout.read close-ready packet: seven-file / 138-pass coverage and deterministic preview PASS.
- **M6**: message `61b7196f-7ac1-43d5-aa29-84592ba6fef4`, 2026-10-18T15:29:20.557759+00:00. MCP dialer closed VALID with 13 criteria; reviewer and run UUIDs supplied.
- **M7**: message `f54aed5e-3092-4acb-b966-bd19c8f15b80`, 2026-10-18T17:21:08.876350+00:00. Node spool close VALID, terminal and drain recorded; two nonblocking findings retained.
- **M8**: message `24d68420-8e04-4b82-9fe9-cd17e8a140d5`, 2026-10-18T16:59:40.266883+00:00. Detection manifest preview blocked on six scope paths and additional type/test coverage.
- **M9**: message `5f028b49-b427-4e49-9543-432a5e9bbbec`, 2026-10-18T19:31:28.379164+00:00. Ledger cleanup close admission after exact-tree landing and activation.
- **M10**: message `54c1a135-f689-4902-8abf-780614437121`, 2026-10-18T21:17:33.932697+00:00. Durable node state preview admission after four-file installer coverage, 93 PASS.
- **M11**: message `f29261b9-29bb-49f9-8906-866e4b43d528`, 2026-10-18T21:12:14.836822+00:00. Review-gate close refused Gate 10: prior 310 PASS credited but four related files still required.
- **M12**: message `786a4933-e30a-4337-8385-314cdddf74d8`, 2026-10-18T22:02:40.966357+00:00. Temp-root docs close-review started; exact reviewer/run UUIDs and consumed GO recorded.
- **M13**: message `88de21c0-2a9c-47ad-960f-b5df524f6f2b`, 2026-10-18T21:18:06.239184+00:00. Rust tmux close refused on tagged ancestor links and added validation scope; no reviewer spawned.
- **M14**: message `f7186340-3d07-42d1-9353-f7a5313afecb`, 2026-10-18T05:16:33.811407+00:00. Gate 12 tightening initial close admission after LM release; retry prohibited on preview failure.
- **M15**: message `68ef219a-df13-41bd-af1d-d8e6f2616c10`, 2026-10-18T08:06:58.579125+00:00. Native variable authority preview refused Gate 7 ancestor linkage; no reviewer spawned.
- **M16**: message `06270c92-2b4b-4ed5-93af-0f6155cc2e16`, 2026-10-18T19:32:02.068313+00:00. Gate 12 stub close blocked for 88.7 seconds; stale types, prior tsc failure and integration-credit mismatch.
- **M17**: message `84a55f58-0764-48bf-aa90-595c66ef5a46`, 2026-10-18T20:52:46.711189+00:00. Gate 12 transcript RED preview lacked fresh credit for eight additional related targets.
- **M18**: message `35f5780d-46a5-4673-9fa3-76e0d546540b`, 2026-10-18T20:06:40.670168+00:00. Node routes landed 26-file validation: 396 PASS / 5 FAIL, all failures in the active gcode surface.
- **M19**: message `9020faa3-e741-4834-908f-2134148b6930`, 2026-10-18T20:18:59.189423+00:00. Seat recovery landing and 2168-pass / 1975.73-second retest; close admission issued.
- **M20**: message `089212d8-b068-4c32-a49c-4c84d95fc7d0`, 2026-10-18T16:56:04.606194+00:00. Caller machine scope preview refused Gate 12 with six nodes lacking attributable RED evidence.
- **M21**: message `35506dc5-b6e0-46bc-991c-a2b5d2e84009`, 2026-10-18T21:45:50.569640+00:00. gcode Git cost preview admission; eight-file / 135-pass Gate 10 coverage retained with qualified exit.

Additional validation endpoints: #24647 Daemon recovery fresh 18-file / 811-pass admission and 175-pass supplemental coverage are in messages `2003142e-e0bc-4db7-8bbb-4ef1893a7f9f` and `dec5f3c7-9ef6-4440-8dce-bb70f1496b32`; #24350 MCP dialer covering-nine result is `9e76ab37-9999-4cbf-b460-73b2a0b13b5b`; #24631 Gate 12 transcript RED supplementary 339 PASS / 80.89 s is `2c154493-5f40-4bd6-8ff2-e52bc693bcb5`; #24650 Seat recovery invalid criterion-3 close is `2fea9953-ca6b-49f0-9664-65641b2b9821`; #24645 Review-gate first invalid close is `7fe6ec92-3dec-4edc-b250-77287149fa0e`.

## Measurement gaps and approved delivery scope

1. **Full attempt census is not provable.** Tasks/tasks-ops discovery exposed no read tool for task events or close-review rows. No direct DB fallback is permitted. A task's current status/updated_at is not a history of unsuccessful previews. #24595 Caller machine scope has a documented 16:56 UTC preview today while its returned updated_at is 02:59 UTC, outside today's inventory filter. The 23-task union is therefore a lower bound, not a completeness proof.
2. **Requested intervals without endpoints remain U.** First candidate stamps span rework, receipt tuples omit UUIDs, agent results omit queue entry/promotion, and sampled RM occupancy cannot yield exact resource-time totals. No absence was converted to zero.
3. **Excluded owner data remains excluded.** #24622 Native WS has no established close attempt in the permitted evidence at cutoff; source validation and reviewer assignment are not themselves close attempts. Coordinator/RM evidence alone governs inclusion.
4. **Activation receipt lag is not pure cutover delay.** Tests, linkage repair, version collision and batching may lie in the same interval. A state-event export is needed for exclusive attribution.
5. **Approved delivery scope.** ORCH's 17:29 CDT ruling authorizes this artifact and a CANDIDATE with the missing read surface stated as a finding. It does not turn the 23-task lower bound into a complete census or remove the unobservable intervals. A future attempt export should be reconciled against this cohort before a full-day completeness claim.

_Last verified: 2026-10-18_
