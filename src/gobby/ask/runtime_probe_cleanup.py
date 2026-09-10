"""Validate captured native probe host and descendant cleanup evidence."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any


def _rows(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError("Ask runtime process-group evidence is invalid")
    return value


def _launch_group(row: Mapping[str, Any], pid: object) -> None:
    if type(pid) is not int or pid <= 0 or row.get("pgid") != pid:
        raise ValueError("Ask runtime launch group identity is invalid")
    group = _rows(row.get("process_group"))
    members: dict[int, str] = {}
    for member in group:
        member_pid, start = member.get("pid"), member.get("start_identity")
        if (
            type(member_pid) is not int
            or member_pid <= 0
            or member.get("pgid") != pid
            or not isinstance(start, str)
            or not start
            or member_pid in members
        ):
            raise ValueError("Ask runtime launch group member is invalid")
        members[member_pid] = start
    if pid not in members or members[pid] != row.get("start_identity"):
        raise ValueError("Ask runtime launch group lacks its captured leader")


def _host_identity(row: Mapping[str, Any]) -> tuple[object, ...]:
    pid = row.get("host_pid")
    if type(pid) is not int or pid <= 0 or row.get("pgid") != pid:
        raise ValueError("Ask runtime host process identity is invalid")
    keys = (
        "start_identity",
        "host_epoch",
        "socket_dir",
        "control_socket",
        "frames_socket",
        "pidfile",
    )
    values = [row.get(key) for key in keys]
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError("Ask runtime host receipt identity is incomplete")
    directory = Path(row["socket_dir"])
    paths = [Path(row[key]) for key in ("control_socket", "frames_socket", "pidfile")]
    if (
        not directory.is_absolute()
        or len(set(paths)) != 3
        or any(path.parent != directory or ".." in path.parts for path in paths)
    ):
        raise ValueError("Ask runtime host socket identity is invalid")
    return (pid, *values)


def validate_probe_group_cleanup(process_sets: Mapping[str, Any]) -> None:
    """Require launch authority and final absence for hosts and every agent group."""
    snapshots = list(process_sets.values())
    if any(not isinstance(snapshot, dict) for snapshot in snapshots):
        raise ValueError("Ask runtime process snapshots are invalid")
    cleanup = process_sets.get("after_cleanup")
    if not isinstance(cleanup, dict):
        raise ValueError("Ask runtime final group cleanup is missing")
    final_hosts = _rows(cleanup.get("hosts"))
    final_host_ids = set()
    for host in final_hosts:
        identity = _host_identity(host)
        if (
            identity in final_host_ids
            or host.get("live") is not False
            or host.get("process_group") != []
            or host.get("control_socket_exists") is not False
            or host.get("frames_socket_exists") is not False
        ):
            raise ValueError("Ask runtime host group or sockets remain live or unknown")
        final_host_ids.add(identity)
    workers = {
        row.get("pid")
        for snapshot in snapshots
        for row in _rows(snapshot.get("workers"))
        if row.get("live") is True
    }
    launched_hosts = set()
    for phase in ("fresh", "resumed", "recovery"):
        snapshot = process_sets.get(f"{phase}_host_launch")
        if not isinstance(snapshot, dict):
            raise ValueError("Ask runtime host phase launch is missing")
        hosts = _rows(snapshot.get("hosts"))
        if len(hosts) != 1:
            raise ValueError("Ask runtime host phase launch is ambiguous")
        host = hosts[0]
        identity = _host_identity(host)
        expected_phase = "recover" if phase == "recovery" else phase
        if (
            host.get("phase") != expected_phase
            or host.get("worker_pid") not in workers
            or host.get("live") is not True
            or host.get("control_socket_exists") is not True
            or host.get("frames_socket_exists") is not True
            or type(host.get("adopted")) is not bool
            or type(host.get("spawned_this_construction")) is not bool
            or host["adopted"] == host["spawned_this_construction"]
        ):
            raise ValueError("Ask runtime host launch lacks a live worker or socket receipt")
        _launch_group(host, host["host_pid"])
        launched_hosts.add(identity)
    if launched_hosts != final_host_ids:
        raise ValueError("Ask runtime host cleanup does not cover its launches")

    launched_agents: set[tuple[object, ...]] = set()
    live_agents: set[tuple[object, ...]] = set()
    for snapshot in snapshots:
        for agent in _rows(snapshot.get("agents")):
            identity = (agent.get("id"), agent.get("pid"), agent.get("start_identity"))
            if agent.get("live") is True:
                live_agents.add(identity)
            group = agent.get("process_group")
            if group:
                # An after-leader snapshot can contain only surviving descendants;
                # authority comes from an earlier receipt that includes the leader.
                members = _rows(group)
                if any(row.get("pid") == agent.get("pid") for row in members):
                    _launch_group(agent, agent.get("pid"))
                    launched_agents.add(identity)
    final_agents = _rows(cleanup.get("agents"))
    final_agent_ids = set()
    for agent in final_agents:
        if agent.get("pid") is None and agent.get("live") is False:
            continue
        if agent.get("live") is not False or agent.get("process_group") != []:
            raise ValueError("Ask runtime agent group remains live or unknown")
        final_agent_ids.add((agent.get("id"), agent.get("pid"), agent.get("start_identity")))
    if not live_agents.issubset(launched_agents) or not launched_agents.issubset(final_agent_ids):
        raise ValueError("Ask runtime agent group lacks launch or final cleanup evidence")
