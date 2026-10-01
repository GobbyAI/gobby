"""Bounded, synthetic, out-of-process #22910 recall benchmark.

Run only with Lane Manager resource admission. No production instrumentation is
installed. Each arm imports an immutable Git archive, owns a disposable test-hub
schema and a private unauthenticated loopback FalkorDB container of the same
namespace, and serves the source health route on a fresh socket.
The parent measures health over HTTP while the child runs the real SearchService.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import math
import os
import queue
import random
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import ExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch
from urllib.parse import urlparse

TEST_DSN = "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test"
QUERY = "Memory graph recall"
PROJECT_ID = "22910000-0000-4000-8000-000000000001"
BEFORE = "7092285b9cd8ddd5d4df8bc143bd469ac6e0a767"
AFTER = "df18d5493fba5d39769cc26f8d2ded53a4b0702f"
STAMP_TIME = "2026-01-01T00:00:00+00:00"


def emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True, default=str), flush=True)


def unit(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in values))
    if not norm:
        raise ValueError("Embedding must have a nonzero norm")
    return [value / norm for value in values]


def corpus_vectors(base: list[float], size: int, near_duplicates: bool) -> list[list[float]]:
    """Controlled geometry: independent directions or eight duplicate groups.

    Corpus vectors are fixtures, not measured provider-generated memory vectors.
    All are close enough to the query to reach the real vector-search boundary.
    """
    rng = random.Random(22910)
    directions: list[list[float]] = []
    for _ in range(8 if near_duplicates else size):
        noise = [rng.gauss(0, 1) for _ in base]
        dot = sum(a * b for a, b in zip(base, noise, strict=True))
        directions.append(unit([b - dot * a for a, b in zip(base, noise, strict=True)]))
    vectors = []
    for index in range(size):
        group = index * 8 // size if near_duplicates else index
        # Group-local perturbations stay far inside the source duplicate threshold;
        # distinct group directions remain outside it.
        vector = [0.92 * a + 0.392 * b for a, b in zip(base, directions[group], strict=True)]
        vector[index % len(base)] += index * 0.000001
        vectors.append(unit(vector))
    return vectors


def inferred_pairs(ordered: list[Any], vectors: dict[str, list[float]], kept: list[Any]) -> int:
    """Infer executed comparisons from the greedy fold's actual representatives."""
    representative = {
        duplicate: memory.id for memory in kept for duplicate in memory.collapsed_duplicates or []
    }
    seen: list[str] = []
    pairs = 0
    for memory in ordered:
        if memory.id not in vectors:
            continue
        target = representative.get(memory.id)
        if target is None:
            pairs += len(seen)
            seen.append(memory.id)
        else:
            pairs += seen.index(target) + 1
    return pairs


@dataclass
class Trace:
    events: list[dict[str, Any]] = field(default_factory=list)
    retained: list[tuple[dict[str, Any], Any]] = field(default_factory=list)
    round: int = 0
    candidate_limit: int = 0

    def record(self, leg: str, start: int, result: Any, error: str | None = None) -> None:
        event = {
            "leg": leg,
            "round": self.round,
            "candidate_limit": self.candidate_limit,
            "elapsed_ms": (time.perf_counter_ns() - start) / 1e6,
            "returned": len(result) if isinstance(result, (list, dict, tuple)) else None,
            "error": error,
        }
        self.events.append(event)
        if leg.startswith("graph_query.") or leg == "embed":
            self.retained.append((event, result))

    def async_wrapper(self, original: Callable[..., Any], leg: str) -> Callable[..., Any]:
        @wraps(original)
        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter_ns()
            try:
                result = await original(*args, **kwargs)
            except BaseException as exc:
                self.record(leg, start, None, type(exc).__name__)
                raise
            self.record(leg, start, result)
            return result

        return wrapped

    def sync_wrapper(self, original: Callable[..., Any], leg: str) -> Callable[..., Any]:
        @wraps(original)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter_ns()
            try:
                result = original(*args, **kwargs)
            except BaseException as exc:
                self.record(leg, start, None, type(exc).__name__)
                raise
            self.record(leg, start, result)
            if leg == "collapse":
                # Compute counts and serialize raw responses only after the timed
                # search has ended, avoiding per-comparison instrumentation.
                self.retained.append((self.events[-1], (args[0], args[1], result)))
            return result

        return wrapped

    def finalize(self) -> None:
        for event, result in self.retained:
            if event["leg"] == "collapse":
                ordered, vectors, kept = result
                event["cosine_pairs_inferred"] = inferred_pairs(ordered, vectors, kept)
                event["candidate_vectors"] = len(vectors)
                event["ordered_candidates"] = len(ordered)
            elif event["leg"] == "embed":
                event["vector_sha256"] = hashlib.sha256(json.dumps(result).encode()).hexdigest()
            else:
                event["response_json_bytes"] = len(
                    json.dumps(result, default=str, separators=(",", ":")).encode()
                )


@contextmanager
def instrument(service: Any, reader: Any, vector: Any, trace: Trace) -> Iterator[None]:
    from gobby.memory.services import _search_paths, _search_results

    with ExitStack() as stack:
        for owner, name, leg in (
            (service, "_embed_fn", "embed"),
            (vector, "search", "vector"),
            (service, "_search_graph_scored", "graph"),
            (reader, "search_entities_by_vector", "graph_vector_and_direct_mentions"),
            (reader, "find_related_memory_ids", "graph_related_expansion"),
            (service, "_keyword_ranked", "keyword"),
            (_search_paths, "_score_unwindowed_candidates", "rescore"),
            (_search_paths, "_candidate_vectors", "candidate_vectors"),
        ):
            stack.enter_context(
                patch.object(owner, name, trace.async_wrapper(getattr(owner, name), leg))
            )
        for owner, name, leg in (
            (service, "_keyword_search", "keyword_sql"),
            (service, "_build_results", "build_results"),
            (_search_results, "collapse_near_duplicates", "collapse"),
        ):
            stack.enter_context(
                patch.object(owner, name, trace.sync_wrapper(getattr(owner, name), leg))
            )

        original_query = reader._falkor.query

        async def query(cypher: str, params: dict[str, Any] | None = None) -> Any:
            params = params or {}
            kind = (
                "raw_hop"
                if "source_keys" in params
                else "vector"
                if "embedding" in params
                else "component"
                if "memory_ids" in params
                else "mentions"
            )
            return await trace.async_wrapper(original_query, f"graph_query.{kind}")(cypher, params)

        stack.enter_context(patch.object(reader._falkor, "query", query))
        original_collect = service._collect_active_results

        async def collect_active(*, limit: int, collect: Any, build: Any) -> Any:
            async def one_round(candidate_limit: int) -> Any:
                trace.round += 1
                trace.candidate_limit = candidate_limit
                candidates = await trace.async_wrapper(collect, "collect")(candidate_limit)
                trace.events[-1].update(
                    merged_candidates=len(candidates.merged_ids),
                    exhausted=candidates.exhausted,
                )
                return candidates

            return await original_collect(limit=limit, collect=one_round, build=build)

        stack.enter_context(patch.object(service, "_collect_active_results", collect_active))
        yield


@contextmanager
def database_schema(schema: str) -> Iterator[Any]:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    from gobby.storage.hub.postgres import PostgresHubDatabase

    if os.environ.get("DATABASE_URL") != TEST_DSN or os.environ.get("GOBBY_TEST_PROTECT") != "1":
        raise ValueError("Only the explicitly protected loopback test hub is permitted")
    with psycopg.connect(TEST_DSN, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        db = PostgresHubDatabase(
            make_conninfo(
                TEST_DSN,
                options=f"-c search_path={schema},public -c statement_timeout=5000 -c lock_timeout=5000",
            )
        )
        try:
            db.execute("""
                CREATE TABLE memories (
                    id TEXT PRIMARY KEY, content TEXT NOT NULL, tags_text TEXT NOT NULL,
                    tags JSONB NOT NULL, memory_type TEXT NOT NULL, project_id TEXT NOT NULL,
                    is_global BOOLEAN NOT NULL DEFAULT FALSE, source_type TEXT NOT NULL,
                    source_session_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    access_count INTEGER NOT NULL DEFAULT 0, last_accessed_at TEXT,
                    deleted_at TEXT
                )
            """)
            db.execute("""
                CREATE INDEX memories_search_bm25 ON memories
                USING bm25 (id, content, tags_text, project_id, is_global)
                WITH (key_field='id')
            """)
            emit({"kind": "owned_state", "hub_schema": schema})
            yield db
        finally:
            db.close()
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@asynccontextmanager
async def runtime_database(executor: Any, schema: str) -> AsyncIterator[Any]:
    manager = database_schema(schema)
    db = await executor.run(manager.__enter__)
    try:
        yield db
    finally:
        exception = sys.exc_info()
        # Settle owned DB workers before closing their pool or dropping their
        # schema, including when the 10-second search boundary cancels its await.
        executor.shutdown()
        await asyncio.to_thread(executor.join)
        await asyncio.to_thread(manager.__exit__, *exception)


async def seed_graph(client: Any, ids: list[str], base: list[float]) -> None:
    await client.query("MATCH (n) DETACH DELETE n")
    await client.ensure_memory_graph_schema()
    seeds = [{"key": f"seed{index}", "name": f"Synthetic seed {index}"} for index in range(8)]
    await client.query(
        "UNWIND $seeds AS s CREATE (e:_Entity {entity_key:s.key, name:s.name, "
        "entity_type:'concept', project_id:$project, is_global:false})",
        {"seeds": seeds, "project": PROJECT_ID},
    )
    for seed in seeds:
        await client.set_node_vector(seed["key"], base)
    neighbors = [
        {
            "seed": f"seed{seed}",
            "key": f"neighbor{seed}_{neighbor:02d}",
            "weight": 1 - neighbor / 1000,
        }
        for seed in range(8)
        for neighbor in range(64)
    ]
    await client.query(
        "UNWIND $rows AS row MATCH (s:_Entity {entity_key:row.seed}) "
        "CREATE (n:_Entity {entity_key:row.key, name:row.key, entity_type:'concept', "
        "project_id:$project, is_global:false}) "
        "CREATE (s)-[:SUPPORTS {weight:row.weight,support:2,updated_at:1767225600000}]->(n)",
        {"rows": neighbors, "project": PROJECT_ID},
    )
    mentions = [
        {
            "id": mid,
            "seed": f"seed{index % 8}",
            "neighbor": f"neighbor{index % 8}_{index % 8:02d}",
            "updated": f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}+00:00",
        }
        for index, mid in enumerate(ids)
    ]
    await client.query(
        "UNWIND $rows AS row MATCH (s:_Entity {entity_key:row.seed}), "
        "(n:_Entity {entity_key:row.neighbor}) "
        "CREATE (m:Memory {memory_id:row.id, project_id:$project, is_global:false,updated_at:row.updated}) "
        "CREATE (s)-[:MENTIONED_IN]->(m) CREATE (n)-[:MENTIONED_IN]->(m)",
        {"rows": mentions, "project": PROJECT_ID},
    )


async def worker(args: argparse.Namespace) -> None:
    import importlib

    import uvicorn
    from fastapi import FastAPI

    # Sources before the runtime_compat import-cycle fix fail when that module is
    # the first gobby import; load gobby.cli first, as the installed daemon does.
    importlib.import_module("gobby.cli")
    from gobby.ai.embeddings import EmbeddingService
    from gobby.config.persistence import MemoryConfig
    from gobby.hooks import runtime_compat
    from gobby.memory.falkor_client import FalkorClient
    from gobby.memory.services.keyword import MemoryKeywordSearchService
    from gobby.memory.services.knowledge_graph.reader import KnowledgeGraphReader
    from gobby.memory.services.search import SearchService
    from gobby.memory.vectorstore import VectorStore
    from gobby.servers.routes.admin import _health
    from gobby.storage.executor import DatabaseExecutor
    from gobby.storage.memories import LocalMemoryManager

    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("Worker requires an asyncio task")
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    state = Path(os.environ["GOBBY_HOME"])
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    embedder = EmbeddingService(
        model=args.model, api_base=args.api_base, dim=args.dim, query_prefix=args.query_prefix
    )
    seed_path = Path(args.seed)
    if seed_path.exists():
        base = json.loads(seed_path.read_text())["vector"]
    else:
        base = (
            await embedder.generate_embedding(QUERY, is_query=True, max_retries=1)
            if args.embedding_mode == "local"
            else unit([1.0] + [0.0] * (args.dim - 1))
        )
        seed_path.write_text(json.dumps({"vector": base}))
    base = unit(base)
    if len(base) != args.dim:
        raise ValueError("Configured embedding dimension differs from seed vector")

    async def embed(text: str, **kwargs: Any) -> list[float]:
        if args.embedding_mode == "fixture":
            return base.copy()
        return await embedder.generate_embedding(text, max_retries=1, **kwargs)

    stamp = state / "ghook-runtime.json"
    stamp.write_text(
        json.dumps(
            {
                "schema_version": runtime_compat.SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION,
                "ghook_version": runtime_compat.MINIMUM_GHOOK_VERSION_FOR_SUPPORTED_SCHEMA,
            }
        )
    )

    class HealthContext:
        def get_runner(self) -> None:
            return None

    app = FastAPI()
    with (
        patch.object(_health, "get_install_dir", lambda: state),
        patch.object(
            _health,
            "read_ghook_runtime_diagnostic",
            lambda: runtime_compat.read_ghook_runtime_diagnostic(stamp),
        ),
    ):
        # The route's globals are looked up on every request, so retain these
        # external-state adapters for the whole server lifetime.
        app.include_router(_health.create_health_router(cast(Any, HealthContext())))
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="error", access_log=False, lifespan="off")
        )
        server_task = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started:
            if server_task.done():
                await server_task
                raise RuntimeError("Source health server failed to start")
            await asyncio.sleep(0.01)
        emit(
            {
                "kind": "ready",
                "port": sock.getsockname()[1],
                "source_module": str(Path(_health.__file__).resolve()),
            }
        )
        if (await asyncio.to_thread(sys.stdin.readline)).strip() != "go":
            raise ValueError("Parent did not admit the benchmark start")

        executor = DatabaseExecutor(max_workers=4, thread_name_prefix="gobby-22910-db")
        graph_name = args.namespace
        client = FalkorClient(
            host="127.0.0.1", port=args.falkor_port, graph_name=graph_name, timeout=5
        )
        emit({"kind": "owned_state", "falkor_graph": graph_name})
        try:
            async with runtime_database(executor, args.namespace) as db:
                for size in args.sizes:
                    for near in (False, True):
                        case = f"{'near_duplicates' if near else 'distinct'}_{size}"
                        ids = [
                            str(uuid.uuid5(uuid.NAMESPACE_DNS, f"gobby22910-{case}-{index}"))
                            for index in range(size)
                        ]
                        vectors = corpus_vectors(base, size, near)
                        await executor.run(db.execute, "TRUNCATE memories")
                        await executor.run(
                            db.executemany,
                            "INSERT INTO memories (id, content, tags_text, tags, memory_type, "
                            "project_id, source_type, created_at, updated_at) VALUES "
                            "(%s,%s,%s,'[]'::jsonb,'fact',%s,'agent',%s,%s)",
                            [
                                (
                                    mid,
                                    f"Memory graph recall synthetic {index}",
                                    "",
                                    PROJECT_ID,
                                    STAMP_TIME,
                                    STAMP_TIME,
                                )
                                for index, mid in enumerate(ids)
                            ],
                        )
                        await seed_graph(client, ids, base)
                        vector = VectorStore(
                            path=str(state / case), collection_name=case, embedding_dim=args.dim
                        )
                        await vector.initialize()
                        try:
                            for mid, values in zip(ids, vectors, strict=True):
                                await vector.upsert(
                                    mid,
                                    values,
                                    payload={
                                        "project_id": PROJECT_ID,
                                        "is_global": False,
                                        "memory_type": "fact",
                                    },
                                )
                            reader = KnowledgeGraphReader(client, embed, embedding_dim=args.dim)
                            await reader.ensure_vector_index()
                            keyword = MemoryKeywordSearchService(db)

                            def vector_failure(message: str, error: BaseException) -> None:
                                raise RuntimeError(message) from error

                            service = SearchService(
                                storage=LocalMemoryManager(db),
                                vector_store=vector,
                                embed_fn=embed,
                                kg_service=cast(Any, reader),
                                keyword_search=keyword.search,
                                config=MemoryConfig(temporal_decay_half_life_days=0),
                                falkordb_graph_search=True,
                                falkordb_graph_min_score=0.5,
                                rrf_k=60,
                                falkordb_rrf_k=60,
                                vector_store_failure_logger=vector_failure,
                                run_db=executor.run,
                            )
                            limit = size // 8 if near else size
                            for repetition in range(-1, args.repetitions):
                                embedder.clear_cache()
                                trace = Trace()
                                start = time.perf_counter_ns()
                                with instrument(service, reader, vector, trace):
                                    async with asyncio.timeout(10):
                                        memories = await service.search(
                                            QUERY,
                                            project_id=PROJECT_ID,
                                            include_global=False,
                                            limit=limit,
                                            caller="benchmark.22910",
                                        )
                                end = time.perf_counter_ns()
                                trace.finalize()
                                errors = [event for event in trace.events if event["error"]]
                                required = {
                                    "embed",
                                    "vector",
                                    "keyword_sql",
                                    "graph_query.raw_hop",
                                    "collapse",
                                }
                                missing = required - {
                                    event["leg"]
                                    for event in trace.events
                                    if event["returned"] is None or event["returned"] > 0
                                }
                                if errors or missing:
                                    raise RuntimeError(
                                        f"Incomplete full-leg run: errors={errors}; missing={missing}"
                                    )
                                expected_count = 8 if near else size
                                if len(memories) != expected_count:
                                    raise RuntimeError(
                                        f"Expected {expected_count} synthetic results, got {len(memories)}"
                                    )
                                payload = [memory.to_dict() for memory in memories]
                                emit(
                                    {
                                        "kind": "search",
                                        "case": case,
                                        "size": size,
                                        "limit": limit,
                                        "repetition": repetition,
                                        "warmup": repetition < 0,
                                        "start_ns": start,
                                        "end_ns": end,
                                        "wall_ms": (end - start) / 1e6,
                                        "results": payload if repetition == 0 else None,
                                        "result_ids": [memory.id for memory in memories],
                                        "result_sha256": hashlib.sha256(
                                            json.dumps(
                                                payload, sort_keys=True, default=str
                                            ).encode()
                                        ).hexdigest(),
                                        "events": trace.events,
                                        "executor": executor.stats().as_dict(),
                                    }
                                )
                        finally:
                            await vector.close()
        finally:
            try:
                await client._execute_command("GRAPH.DELETE", graph_name)
            finally:
                await client.close()
                executor.shutdown()
                await asyncio.to_thread(executor.join)
                server.should_exit = True
                await server_task
                sock.close()
        emit({"kind": "done"})


def health_sample(port: int) -> dict[str, Any]:
    start = time.perf_counter_ns()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
            body = json.load(response)
            status = response.status
        error = None
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        status, body, error = None, None, type(exc).__name__
    end = time.perf_counter_ns()
    return {
        "start_ns": start,
        "end_ns": end,
        "elapsed_ms": (end - start) / 1e6,
        "status": status,
        "payload_status": body.get("status") if isinstance(body, dict) else None,
        "payload_sha256": hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        if body is not None
        else None,
        "error": error,
    }


def run_arm(
    args: argparse.Namespace,
    root: Path,
    revision: str,
    arm: str,
    seed: Path,
    namespace: str,
    falkor_port: int,
) -> dict[str, Any]:
    revision = subprocess.check_output(
        ["git", "-C", str(args.repo), "rev-parse", f"{revision}^{{commit}}"], text=True
    ).strip()
    source_tree = subprocess.check_output(
        ["git", "-C", str(args.repo), "rev-parse", f"{revision}^{{tree}}"], text=True
    ).strip()
    source = root / f"source-{arm}"
    source.mkdir()
    archive = subprocess.check_output(["git", "-C", str(args.repo), "archive", revision, "src"])
    with tarfile.open(fileobj=io.BytesIO(archive)) as files:
        files.extractall(source, filter="data")
    env = dict(
        os.environ,
        PYTHONPATH=str(source / "src"),
        GOBBY_HOME=str(root / f"state-{arm}"),
        DATABASE_URL=TEST_DSN,
        GOBBY_TEST_PROTECT="1",
        RTK_DISABLED="1",
    )
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--namespace",
        namespace,
        "--seed",
        str(seed),
        "--embedding-mode",
        args.embedding_mode,
        "--api-base",
        args.api_base,
        "--model",
        args.model,
        "--dim",
        str(args.dim),
        "--falkor-port",
        str(falkor_port),
        "--repetitions",
        str(args.repetitions),
        "--sizes",
        *map(str, args.sizes),
    ]
    if args.query_prefix is not None:
        command.extend(["--query-prefix", args.query_prefix])
    messages: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    idle: list[dict[str, Any]] = []
    starts: queue.Queue[dict[str, Any]] = queue.Queue()
    ready: dict[str, Any] | None = None
    with (root / f"{arm}.stderr").open("w") as stderr:
        process = subprocess.Popen(
            command,
            cwd=root,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
        )
        assert process.stdout is not None and process.stdin is not None

        def read_messages() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    message = {"kind": "protocol_error", "line": line}
                messages.append(message)
                starts.put(message)
            starts.put({"kind": "eof"})

        reader = threading.Thread(target=read_messages, daemon=True)
        reader.start()
        try:
            startup_deadline = time.monotonic() + 60
            while ready is None or ready.get("kind") not in ("ready", "eof"):
                ready = starts.get(timeout=max(0, startup_deadline - time.monotonic()))
            # The worker reports a resolved path; macOS temp roots sit behind the
            # /var -> /private/var symlink, so compare resolved paths.
            if ready.get("kind") != "ready" or not Path(
                str(ready.get("source_module"))
            ).is_relative_to(source.resolve()):
                messages.append({"kind": "startup_error", "detail": ready})
            else:
                port = int(ready["port"])
                for _ in range(20):
                    idle.append(health_sample(port))
                    time.sleep(0.02)
                process.stdin.write("go\n")
                process.stdin.flush()
                deadline = time.monotonic() + 240
                while process.poll() is None and time.monotonic() < deadline:
                    samples.append(health_sample(port))
                    time.sleep(0.02)
        except queue.Empty:
            messages.append({"kind": "startup_timeout", "seconds": 60})
        finally:
            if process.poll() is None:
                # EOF releases a worker still waiting for "go"; SIGTERM cannot
                # interrupt that thread-bound stdin read.
                process.stdin.close()
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            reader.join(timeout=5)
        return {
            "arm": arm,
            "revision": revision,
            "source_tree": source_tree,
            "source_archive_sha256": hashlib.sha256(archive).hexdigest(),
            "ready": ready,
            "returncode": process.returncode,
            "idle_health": idle,
            "health": samples,
            "messages": messages,
            "provider_http_send_calls": sum(
                message.get("kind") == "provider_http_request" for message in messages
            ),
            "stderr": (root / f"{arm}.stderr").read_text(),
        }


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def summarize(report: dict[str, Any]) -> None:
    paired: dict[str, dict[tuple[str, int], str]] = {}
    for arm in report["arms"]:
        rows = [
            message
            for message in arm["messages"]
            if message.get("kind") == "search" and not message["warmup"]
        ]
        paired[arm["arm"]] = {
            (row["case"], row["repetition"]): row["result_sha256"] for row in rows
        }
        arm["summary"] = []
        for case in sorted({row["case"] for row in rows}):
            searches = [row for row in rows if row["case"] == case]
            health = [
                sample
                for sample in arm["health"]
                if any(
                    sample["start_ns"] < row["end_ns"] and sample["end_ns"] > row["start_ns"]
                    for row in searches
                )
            ]
            legs = sorted({event["leg"] for row in searches for event in row["events"]})
            arm["summary"].append(
                {
                    "case": case,
                    "searches": len(searches),
                    "wall_p50_ms": percentile([row["wall_ms"] for row in searches], 0.5),
                    "wall_p95_ms": percentile([row["wall_ms"] for row in searches], 0.95),
                    "rounds": [
                        [
                            event["candidate_limit"]
                            for event in row["events"]
                            if event["leg"] == "collect"
                        ]
                        for row in searches
                    ],
                    "raw_hop_queries": [
                        sum(event["leg"] == "graph_query.raw_hop" for event in row["events"])
                        for row in searches
                    ],
                    "leg_sum_p50_ms": {
                        leg: percentile(
                            [
                                sum(
                                    event["elapsed_ms"]
                                    for event in row["events"]
                                    if event["leg"] == leg
                                )
                                for row in searches
                            ],
                            0.5,
                        )
                        for leg in legs
                    },
                    "overlapping_health_samples": len(health),
                    "health_errors": sum(
                        sample["error"] is not None or sample["status"] != 200 for sample in health
                    ),
                    "health_p50_ms": percentile([sample["elapsed_ms"] for sample in health], 0.5),
                    "health_p95_ms": percentile([sample["elapsed_ms"] for sample in health], 0.95),
                    "health_max_ms": max((sample["elapsed_ms"] for sample in health), default=None),
                }
            )
    before, after = paired.get("before", {}), paired.get("after", {})
    report["exact_full_result_equivalence"] = bool(before) and before == after
    report["compared_searches_per_arm"] = len(before)


def main() -> None:
    from inline_recall_cleanup import cleanup_check, remove_falkor, start_falkor

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--namespace")
    parser.add_argument("--check-cleanup", type=Path)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--before", default=BEFORE)
    parser.add_argument("--after", default=AFTER)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--seed", default="")
    parser.add_argument("--embedding-mode", choices=("local", "fixture"), default="local")
    parser.add_argument("--api-base", default="http://localhost:1234/v1")
    parser.add_argument("--model", default="nomic-embed-text")
    parser.add_argument("--dim", type=int, default=768)
    parser.add_argument("--query-prefix")
    parser.add_argument("--falkor-port", type=int, help="Worker only; the parent owns it")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--sizes", nargs="+", type=int, default=[80, 240])
    args = parser.parse_args()
    if os.environ.get("DATABASE_URL") != TEST_DSN or os.environ.get("GOBBY_TEST_PROTECT") != "1":
        parser.error("Protected hub60892 is required")
    if args.worker != (args.falkor_port is not None):
        parser.error("Only a worker takes --falkor-port, from its parent's private container")
    if args.check_cleanup is not None:
        cleanup_report = json.loads(args.check_cleanup.read_text())
        cleanup_report["post_run_cleanup_check"] = cleanup_check(cleanup_report)
        args.check_cleanup.write_text(json.dumps(cleanup_report, indent=2, sort_keys=True) + "\n")
        emit(cleanup_report["post_run_cleanup_check"])
        if not cleanup_report["post_run_cleanup_check"]["clean"]:
            raise SystemExit("Benchmark leak check failed")
        return
    if (
        args.dim < 16
        or args.dim > 2048
        or args.repetitions not in range(1, 4)
        or not args.sizes
        or any(size not in (80, 240) for size in args.sizes)
    ):
        parser.error("Bounded workload requires dimension16..2048, repetitions1..3, sizes80/240")
    endpoint = urlparse(args.api_base)
    if endpoint.scheme not in ("http", "https") or endpoint.hostname not in (
        "localhost",
        "127.0.0.1",
        "::1",
    ):
        parser.error("Only the already-admitted loopback embedding endpoint is permitted")
    if args.worker:
        import re

        from httpx import AsyncClient

        if (
            not args.namespace
            or re.fullmatch(r"gobby_test_22910_[0-9a-f]{32}", args.namespace) is None
        ):
            parser.error("Worker requires the parent's exact namespace")
        if not 1024 <= args.falkor_port <= 65535 or args.falkor_port == 16379:
            parser.error("Worker requires the parent's private FalkorDB port, never 16379")
        original_send = AsyncClient.send

        async def observed_send(client: Any, request: Any, **kwargs: Any) -> Any:
            if (
                request.method == "POST"
                and str(request.url) == args.api_base.rstrip("/") + "/embeddings"
            ):
                emit({"kind": "provider_http_request"})
            return await original_send(client, request, **kwargs)

        with patch.object(AsyncClient, "send", observed_send):
            asyncio.run(worker(args))
        return
    if args.output is None:
        parser.error("--output is required so raw evidence is retained")
    if args.output.exists():
        parser.error(f"--output {args.output} exists; use a fresh name per attempt")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "schema_version": 1,
        "task": "#22910 Inline memory surfacing",
        "query": QUERY,
        "embedding_mode": args.embedding_mode,
        "embedding": {
            "api_base": args.api_base,
            "model": args.model,
            "dim": args.dim,
            "query_prefix": args.query_prefix,
        },
        "limits": {
            "request_cap_seconds": 10,
            "arm_deadline_seconds": 240,
            "sizes": args.sizes,
            "repetitions": args.repetitions,
        },
        "method": {
            "source": "Separate processes importing exact immutable Git archives",
            "corpus": "Synthetic content and controlled vectors; query embedding uses selected mode",
            "timers": "Nested/overlapping wall spans; do not add leg times",
            "counts": "Raw calls/rows measured; cosine comparison counts inferred from actual greedy folds",
            "control_plane": "External HTTP source-health samples; minimal isolated app, not full runner",
            "state": "Owned hub60892 schema, private per-arm FalkorDB container, temporary local Qdrant/GOBBY_HOME",
            "warmup": "One excluded warmup per case; embedding cache cleared before every request",
            "claims": "Synthetic matched source comparison; no installed-runtime or cluster causality claim",
        },
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "arms": [],
        "falkor": [],
        "namespaces": [],
    }
    try:
        with tempfile.TemporaryDirectory(prefix="gobby-22910-") as directory:
            root = Path(directory)
            report["temporary_directory"] = directory
            seed = root / "seed.json"
            for arm, revision in (("before", args.before), ("after", args.after)):
                namespace = f"gobby_test_22910_{uuid.uuid4().hex}"
                report["namespaces"].append(namespace)
                args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
                try:
                    # Fresh private FalkorDB per arm: no live secret, no live-KG
                    # contention, and identical cold state for both sources.
                    falkor = start_falkor(namespace)
                    report["falkor"].append(falkor)
                    report["arms"].append(
                        run_arm(args, root, revision, arm, seed, namespace, falkor["port"])
                    )
                finally:
                    remove_falkor(namespace)
                args.output.write_text(
                    json.dumps(report, indent=2, sort_keys=True, default=str) + "\n"
                )
                if report["arms"][-1]["returncode"] != 0:
                    break
    finally:
        if report["namespaces"]:
            report["cleanup"] = cleanup_check(report)
        report["provider_http_send_calls"] = sum(
            arm["provider_http_send_calls"] for arm in report["arms"]
        )
        summarize(report)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")
    failed = [arm["arm"] for arm in report["arms"] if arm["returncode"] != 0]
    if failed:
        raise SystemExit(f"Incomplete benchmark arms: {failed}; retained {args.output}")
    if not report.get("cleanup", {}).get("clean"):
        raise SystemExit(f"Benchmark cleanup incomplete; retained {args.output}")
    if not report["exact_full_result_equivalence"]:
        raise SystemExit(f"Full result digests differ or are absent; retained {args.output}")
    emit({"kind": "complete", "output": str(args.output), "arms": len(report["arms"])})


if __name__ == "__main__":
    main()
