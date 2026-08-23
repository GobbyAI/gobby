"""Request-balanced sampling for shadow-label audits."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from typing import Any


class RecallShadowSamplingMixin:
    """Ship and diagnostic sampling over admitted shadow replay rows."""

    def fetch_shadow_replay_rows(
        self,
        *,
        label_source: str,
        candidate_scope: str,
        judge_protocol_version: str,
        query_construction_version: str | None,
        weighting_regime_key: str,
        judge_model_key: str,
        judge_config_fingerprint: str,
        data_cutoff: datetime,
        completion_cutoff: datetime,
        project_id: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def sample_usefulness_labels(
        self,
        *,
        label_source: str,
        protocol_version: str,
        query_construction_version: str | None,
        judge_model_key: str,
        judge_config_fingerprint: str,
        regime_key: str,
        candidate_scope: str,
        n_requests: int,
        data_cutoff: datetime,
        completion_cutoff: datetime,
        mode: str,
        since: datetime | None = None,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Sample snapshot-bound shadow labels for ship or diagnostic review."""
        if mode not in {"ship", "diagnostic"}:
            raise ValueError("mode must be 'ship' or 'diagnostic'")
        if mode == "ship" and since is not None:
            raise ValueError("since is forbidden in ship sampling")
        if n_requests <= 0:
            return []
        rows = self.fetch_shadow_replay_rows(
            label_source=label_source,
            candidate_scope=candidate_scope,
            judge_protocol_version=protocol_version,
            query_construction_version=query_construction_version,
            weighting_regime_key=regime_key,
            judge_model_key=judge_model_key,
            judge_config_fingerprint=judge_config_fingerprint,
            data_cutoff=data_cutoff,
            completion_cutoff=completion_cutoff,
            project_id=project_id,
            limit=100_000,
        )
        if since is not None:
            rows = [row for row in rows if row["request_created_at"] >= since]
        by_request: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_request.setdefault(str(row["recall_request_id"]), []).append(row)
        if mode == "ship":
            request_ids = sorted(
                by_request,
                key=lambda request_id: self._sample_hash(
                    label_source, protocol_version, request_id
                ),
            )[:n_requests]
            return [
                min(
                    by_request[request_id],
                    key=lambda row: self._sample_hash(request_id, str(row["memory_id"])),
                )
                for request_id in request_ids
            ]

        cells: dict[tuple[str, bool], list[dict[str, Any]]] = {}
        for candidates in by_request.values():
            for row in candidates:
                band = "top" if int(row["rank"]) < 4 else "bottom"
                cells.setdefault((band, bool(row["judge_useful"])), []).append(row)
        for candidates in cells.values():
            candidates.sort(
                key=lambda row: self._sample_hash(
                    str(row["recall_request_id"]), str(row["memory_id"])
                )
            )
        selected: list[dict[str, Any]] = []
        selected_requests: set[str] = set()
        ordered_cells = sorted(cells, key=lambda cell: (len(cells[cell]), cell))
        while len(selected) < n_requests:
            progressed = False
            for cell in ordered_cells:
                candidates = cells[cell]
                while candidates:
                    row = candidates.pop(0)
                    request_id = str(row["recall_request_id"])
                    if request_id in selected_requests:
                        continue
                    item = dict(row)
                    item["diagnostic_cell"] = cell
                    selected.append(item)
                    selected_requests.add(request_id)
                    progressed = True
                    break
                if len(selected) >= n_requests:
                    break
            if not progressed:
                break
        return selected

    @staticmethod
    def _sample_hash(*parts: str) -> str:
        return sha256("\0".join(parts).encode("utf-8")).hexdigest()
