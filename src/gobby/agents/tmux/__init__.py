"""Tmux helpers for hand-started external panes.

Gobby spawns no tmux sessions (#22856). ``TmuxSessionManager`` drives the
spawn-less ``TmuxTerminalRuntime`` adapter, which reaches panes a user started
for send_keys, wake, capture, and liveness probes.
"""
