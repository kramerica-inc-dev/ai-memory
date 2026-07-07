"""Durable episode queue for the Graphiti MCP server — replaces services/queue_service.py.

Why (three production incidents in two days):
  1. The stock queue is a plain asyncio.Queue: a container restart silently drops
     every queued episode (the ledger/UX already said "queued OK").
  2. Provider rate limits (429/quota) dropped episodes after two sub-second retries.
  3. Concurrent per-group workers corrupted graphs cross-group because graphiti-core
     0.28.2 mutates the shared driver per group_id (graphiti.py:889).

This replacement keeps the exact public interface of the original QueueService but:
  - persists episodes in a Redis STREAM (`aimem:queue`) in the same FalkorDB/Redis the
    graphs live in (AOF-durable) — a restart resumes exactly where it left off via the
    consumer-group pending list;
  - processes with ONE global consumer → strict serialization across all groups, which
    removes the cross-group corruption class entirely (no shared-driver interleaving);
  - deduplicates via an idempotency key (sha256 of group|name|source_description|content)
    tracked in `aimem:processed` — re-submitting the same episode is a no-op;
  - retries transient failures (rate limit / timeout / connection) with real backoff,
    and parks poison episodes in a dead-letter stream (`aimem:dead`) after MAX_ATTEMPTS
    instead of losing them — inspect/replay with XRANGE / a small script;
  - enforces the hygiene gate on the MCP write path when SANITIZER_URL is set:
    episode content is scrubbed (secrets/PII) at consume time, BEFORE it reaches the
    extraction LLM or the graph. Fail-closed: a sanitizer failure is a transient
    error (backoff → dead-letter), never an unscrubbed pass-through.

Installed by the Dockerfile as /app/mcp/src/services/queue_service.py. The companion
patch-durable-queue.py passes entity_types to initialize() at the server call site.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import urllib.request
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import redis.asyncio as aioredis
from graphiti_core.nodes import EpisodeType

logger = logging.getLogger(__name__)

STREAM = 'aimem:queue'
DEAD = 'aimem:dead'
PROCESSED = 'aimem:processed'
GROUP = 'workers'
CONSUMER = 'main'
MAX_ATTEMPTS = int(os.environ.get('QUEUE_MAX_ATTEMPTS', '5'))
TRANSIENT_MARKERS = ('rate limit', '429', 'timeout', 'timed out', 'connection',
                     'overloaded', '503', '529', 'temporarily', 'sanitizer')
SANITIZER_URL = os.environ.get('SANITIZER_URL', '').rstrip('/')
SANITIZER_DEPTH = os.environ.get('SANITIZER_DEPTH', 'quick')


def _idempotency_key(group_id: str, name: str, source_description: str, content: str) -> str:
    raw = f'{group_id}|{name}|{source_description}|{content}'
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]


class QueueService:
    """Durable, idempotent, strictly-serialized episode queue (Redis Streams)."""

    def __init__(self):
        self._graphiti_client: Any = None
        self._entity_types: Any = None
        self._redis: aioredis.Redis | None = None
        self._consumer_task: asyncio.Task | None = None
        # Serializes episode execution; also honored by the add_episode_task shim.
        self._exec_lock = asyncio.Lock()

    # ── wiring ───────────────────────────────────────────────────────────────

    async def initialize(self, graphiti_client: Any, entity_types: Any = None) -> None:
        self._graphiti_client = graphiti_client
        self._entity_types = entity_types
        uri = os.environ.get('FALKORDB_URI', 'redis://falkordb:6379')
        parsed = urlparse(uri)
        self._redis = aioredis.Redis(
            host=parsed.hostname or 'falkordb', port=parsed.port or 6379,
            password=os.environ.get('FALKORDB_PASSWORD') or None,
            decode_responses=True,
        )
        try:
            await self._redis.xgroup_create(STREAM, GROUP, id='0', mkstream=True)
        except aioredis.ResponseError as e:
            if 'BUSYGROUP' not in str(e):
                raise
        self._consumer_task = asyncio.create_task(self._consume())
        backlog = await self._redis.xlen(STREAM)
        logger.info(f'Durable queue service initialized (stream={STREAM}, backlog={backlog})')
        if SANITIZER_URL:
            logger.info(f'Hygiene gate ACTIVE: content sanitized via {SANITIZER_URL} '
                        f'(depth={SANITIZER_DEPTH}) before extraction')
        else:
            logger.warning('SANITIZER_URL not set — MCP episodes are ingested UNSANITIZED '
                           '(secrets/PII reach the extraction LLM and the graph)')

    # ── public interface (parity with the stock QueueService) ───────────────

    async def add_episode(self, group_id: str, name: str, content: str,
                          source_description: str, episode_type: Any,
                          entity_types: Any, uuid: str | None) -> int:
        if self._redis is None:
            raise RuntimeError('Queue service not initialized. Call initialize() first.')
        if entity_types is not None:
            self._entity_types = entity_types
        key = _idempotency_key(group_id, name, source_description, content)
        if await self._redis.sismember(PROCESSED, key):
            logger.info(f'Skipping duplicate episode (idempotency key {key}) for group {group_id}')
            return await self._redis.xlen(STREAM)
        await self._redis.xadd(STREAM, {
            'group_id': group_id, 'name': name, 'content': content,
            'source_description': source_description,
            'source': getattr(episode_type, 'name', str(episode_type)),
            'uuid': uuid or '', 'key': key, 'attempt': '0',
        })
        return await self._redis.xlen(STREAM)

    def get_queue_size(self, group_id: str) -> int:  # noqa: ARG002 — interface parity
        # Async size lives in Redis; expose a cheap best-effort snapshot.
        return 0 if self._redis is None else -1

    def is_worker_running(self, group_id: str) -> bool:  # noqa: ARG002 — interface parity
        return self._consumer_task is not None and not self._consumer_task.done()

    async def add_episode_task(self, group_id: str,
                               process_func: Callable[[], Awaitable[None]]) -> int:
        """Compatibility shim for callers that enqueue a bare closure: execute it
        under the same execution lock so serialization guarantees hold."""
        async with self._exec_lock:
            await process_func()
        return 0

    # ── hygiene gate ─────────────────────────────────────────────────────────

    @staticmethod
    async def _sanitize(text: str) -> str:
        """Scrub secrets/PII before content reaches the extraction LLM or the graph.
        Fail-closed by raising: the error message contains 'sanitizer', which the
        transient classifier matches → backoff and eventually dead-letter, never an
        unscrubbed pass-through."""
        if not SANITIZER_URL or not text.strip():
            return text

        def _call() -> str:
            req = urllib.request.Request(
                f'{SANITIZER_URL}/api/sanitize',
                data=json.dumps({'text': text, 'depth': SANITIZER_DEPTH}).encode('utf-8'),
                headers={'Content-Type': 'application/json'}, method='POST')
            with urllib.request.urlopen(req, timeout=90) as resp:
                body = json.loads(resp.read().decode('utf-8'))
                return body['sanitized']

        try:
            return await asyncio.to_thread(_call)
        except Exception as e:  # noqa: BLE001 — any failure must stay fail-closed
            raise RuntimeError(f'sanitizer failed ({type(e).__name__}): {e}') from e

    # ── consumer ─────────────────────────────────────────────────────────────

    async def _consume(self) -> None:
        assert self._redis is not None
        logger.info('Durable queue consumer started (crash recovery first, then live)')
        cursor = '0'          # '0' = this consumer's pending (pre-crash) entries first
        while True:
            try:
                resp = await self._redis.xreadgroup(
                    GROUP, CONSUMER, {STREAM: cursor}, count=1,
                    block=5000 if cursor == '>' else None)
                if not resp or not resp[0][1]:
                    if cursor != '>':
                        cursor = '>'  # pending backlog drained -> switch to live entries
                        logger.info('Crash-recovery backlog drained; consuming live entries')
                    continue
                entry_id, fields = resp[0][1][0]
                if cursor != '>':
                    cursor = entry_id  # walk the pending list
                await self._handle(entry_id, fields)
            except asyncio.CancelledError:
                logger.info('Durable queue consumer cancelled')
                return
            except Exception as e:  # noqa: BLE001 — consumer must never die
                logger.error(f'Queue consumer error (continuing): {e}')
                await asyncio.sleep(5)

    async def _handle(self, entry_id: str, f: dict) -> None:
        assert self._redis is not None
        group_id, key = f.get('group_id', ''), f.get('key', '')
        attempt = int(f.get('attempt', '0'))
        if key and await self._redis.sismember(PROCESSED, key):
            await self._ack(entry_id)
            return
        try:
            episode_type = EpisodeType[f.get('source', 'text')]
        except KeyError:
            episode_type = EpisodeType.text
        try:
            logger.info(f'Processing episode {f.get("name", "?")[:60]!r} for group {group_id} '
                        f'(attempt {attempt + 1})')
            content = await self._sanitize(f.get('content', ''))
            async with self._exec_lock:
                await self._graphiti_client.add_episode(
                    name=f.get('name', ''),
                    episode_body=content,
                    source_description=f.get('source_description', ''),
                    source=episode_type,
                    group_id=group_id,
                    reference_time=datetime.now(timezone.utc),
                    entity_types=self._entity_types,
                    uuid=f.get('uuid') or None,
                )
            if key:
                await self._redis.sadd(PROCESSED, key)
            await self._ack(entry_id)
            logger.info(f'Successfully processed episode for group {group_id}')
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            transient = any(m in msg.lower() for m in TRANSIENT_MARKERS)
            logger.error(f'Failed to process episode for group_id {group_id} '
                         f'(attempt {attempt + 1}, transient={transient}): {msg[:200]}')
            if attempt + 1 >= MAX_ATTEMPTS:
                await self._redis.xadd(DEAD, {**f, 'error': msg[:500],
                                              'failed_at': datetime.now(timezone.utc).isoformat()})
                await self._ack(entry_id)
                logger.error(f'Episode moved to dead-letter stream {DEAD} after {attempt + 1} attempts')
                return
            # Requeue with attempt+1, then back off — long enough to ride out rate limits.
            await self._redis.xadd(STREAM, {**f, 'attempt': str(attempt + 1)})
            await self._ack(entry_id)
            await asyncio.sleep(min(30 * (attempt + 1), 300) if transient else 5)

    async def _ack(self, entry_id: str) -> None:
        assert self._redis is not None
        await self._redis.xack(STREAM, GROUP, entry_id)
        await self._redis.xdel(STREAM, entry_id)
