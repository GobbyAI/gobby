# Assistant: report handoff

Load before delivering another seat's report. The operator's status report is built and sent by the
daemon, not by this seat.

## Report handoff from another seat

To deliver another seat's report, use the full-content
[artifact transport](../messaging.md#deliver-text-artifacts-across-workspace-boundaries).
Retrieve each complete message by ID, verify all parts and the content digest,
then write the exact text into your own workspace folder. Read
`project_policy.operator_channel`, then call
`gobby-communications:send_operator_attachment(channel=<operator_channel>, file_path=<local copy>)`.
Keep the returned message ID and acknowledge delivery to the sender. A source path
in another seat's private folder is a reference, never a readable attachment.
Report missing parts or delivery errors.

_Version: 2.0.0_

_Last verified: 2026-11-05_
