use serde_json::Value;

/// Check actual emitted client messages against production daemon admission, rather
/// than trusting the transport mock's permissive terminal_write_outcome replies.
pub fn assert_tmux_write_admission(requests: &[Value], terminal_id: &str) {
    let output = std::process::Command::new("uv")
        .args([
            "run",
            "python",
            "-c",
            r#"
import asyncio, json, os
from gobby.terminals.leases import TerminalLeaseRegistry
async def check():
    registry = TerminalLeaseRegistry()
    socket = object()
    terminal = os.environ['GCLIENT_ADMISSION_TERMINAL']
    attaches = 0
    writes = 0
    for message in json.loads(os.environ['GCLIENT_WIRE_REQUESTS']):
        if message.get('terminal_id') != terminal:
            continue
        kind = message.get('type')
        if kind == 'terminal_attach':
            attaches += 1
            await registry.attach(terminal, message['frame_delivery'], websocket=socket,
                                  attachment_id=f'attachment-{attaches}', backend='tmux')
        elif kind == 'terminal_detach':
            await registry.finalize(message['attachment_id'], 'detached')
        elif kind == 'terminal_take_control':
            assert message['takeover'] is False
            await registry.take_control(terminal, message['attachment_id'])
        elif kind == 'terminal_input':
            admitted = registry.admit_write(terminal, websocket=socket,
                attachment_id=message['attachment_id'], expected_lease_generation=None,
                seq=message['client_write_seq'], kind='input', payload=message['data'].encode())
            assert admitted.ok, (message, admitted)
            writes += 1
    assert writes == 1, writes
asyncio.run(check())
"#,
        ])
        .env("GCLIENT_ADMISSION_TERMINAL", terminal_id)
        .env(
            "GCLIENT_WIRE_REQUESTS",
            serde_json::to_string(requests).expect("wire JSON"),
        )
        .output()
        .expect("production admission probe");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
