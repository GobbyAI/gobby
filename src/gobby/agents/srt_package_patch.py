"""Pinned upstream SRT fixes, each removable when an upstream release includes it."""

import hashlib
from pathlib import Path

from gobby.utils.dependency_requirements import SRT_RELEASE

HTTP_PROXY_PATH = Path("node_modules/@anthropic-ai/sandbox-runtime/dist/sandbox/http-proxy.js")
MUX_PROXY_PATH = HTTP_PROXY_PATH.with_name("mux-proxy.js")
MACOS_PROFILE_PATH = HTTP_PROXY_PATH.with_name("macos-sandbox-utils.js")
WHICH_PATH = HTTP_PROXY_PATH.parents[1] / "utils" / "which.js"
_MONITOR_NEEDLE = b"const logProcess = spawn('log', ["
_MONITOR_REPLACEMENT = (
    b"const logProcess = globalThis.gobbySrtLogTransport"
    b" ? globalThis.gobbySrtLogTransport(sessionSuffix) : spawn('log', ["
)
_PATCHES = (
    (
        HTTP_PROXY_PATH,
        "edd5ad21903b9a9b4d091618c55e7e9bee7f6f5135cc58be668c0a5e6deae582",
        SRT_RELEASE.http_proxy_sha256,
        b"upstream.on('close', () => socket.destroy());",
        b"upstream.on('close', () => socket.end());",
    ),
    (
        MUX_PROXY_PATH,
        "07d6eb4bab65e9abcf6e52f45216903cc82cba890139a149a771733b6f50088a",
        SRT_RELEASE.mux_proxy_sha256,
        b"upstream.once('close', () => client.destroy());",
        b"upstream.once('close', () => client.end());",
    ),
    (
        MACOS_PROFILE_PATH,
        "0270e6c93a2be8261c85cdbbe3241434d88f19365201ca2eb1d52a0527c89885",
        SRT_RELEASE.macos_profile_sha256,
        b"return profile.join('\\n');",
        # Read only the multiprocessing semaphore limit. Scheduling remains
        # self-only; the kernel rejects unprivileged priority increases.
        b"return profile.join('\\n')"
        b' + \'\\n(allow sysctl-read (sysctl-name "kern.sysv.semmns"))'
        b"\\n(allow system-sched (target self))';",
    ),
    (
        WHICH_PATH,
        "150f2f55dbb6e74a22a3638732a26bb017dc2559ba6462c630b62d6b0d509076",
        SRT_RELEASE.which_sha256,
        b"""    const result = spawnSync('which', [bin], {
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'ignore'],
        timeout: 1000,
    });
    if (result.status === 0 && result.stdout) {
        return result.stdout.trim();
    }
    return null;
""",
        # A spawned `which` bounded at 1s reports a present shell as missing on
        # a loaded host, failing every wrap closed. Search PATH in-process.
        b"""    const { accessSync, constants, statSync } = process.getBuiltinModule('node:fs');
    const { delimiter, join } = process.getBuiltinModule('node:path');
    const candidates = bin.includes('/')
        ? [bin]
        : (process.env.PATH ?? '').split(delimiter).filter(Boolean).map(dir => join(dir, bin));
    for (const candidate of candidates) {
        try {
            if (statSync(candidate).isFile()) {
                accessSync(candidate, constants.X_OK);
                return candidate;
            }
        }
        catch {
            // Missing or not executable: keep searching, as which(1) does.
        }
    }
    return null;
""",
    ),
)


def verify_srt_proxy_patch(root: Path) -> None:
    """Require the exact patched bytes, independently of the writable manifest."""
    for relative_path, _, expected, _, _ in _PATCHES:
        if hashlib.sha256((root / relative_path).read_bytes()).hexdigest() != expected:
            raise ValueError(
                f"managed SRT {relative_path.name} checksum mismatch; run `gobby install srt`"
            )


def apply_srt_proxy_patch(root: Path) -> None:
    """Patch only verified upstream files in an unpublished staging tree."""
    patches: list[tuple[Path, bytes]] = []
    for relative_path, original_sha256, expected, needle, replacement in _PATCHES:
        path = root / relative_path
        original = path.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        if digest == expected:
            continue
        if digest != original_sha256 or original.count(needle) != 1:
            raise ValueError(
                f"managed SRT {relative_path.name} does not match the pinned upstream source"
            )
        patched = original.replace(needle, replacement)
        if relative_path == MACOS_PROFILE_PATH:
            if patched.count(_MONITOR_NEEDLE) != 1:
                raise ValueError("managed SRT macOS monitor does not match pinned source")
            patched = patched.replace(_MONITOR_NEEDLE, _MONITOR_REPLACEMENT)
        if hashlib.sha256(patched).hexdigest() != expected:
            raise ValueError(f"managed SRT {relative_path.name} patch checksum mismatch")
        patches.append((path, patched))
    for path, patched in patches:
        path.write_bytes(patched)
