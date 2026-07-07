"""
InfinityRerankerClient — local cross-encoder reranking via an infinity server.

Replaces graphiti-core's default OpenAIRerankerClient (the last unconditional cloud
dependency in the stack) with the already-running infinity container: it serves a
reranker model (e.g. BAAI/bge-reranker-v2-m3, same family as the bge-m3 embedder,
multilingual) over the OpenAI-ish /rerank endpoint. No new service, no torch inside
the MCP image.

Failure mode is deliberately graceful: when the reranker is unreachable or errors,
passages are returned in their original order with neutral scores — search degrades
to unranked instead of failing (strictly better than the default client, which
errors the whole search when its provider is down).

Wired in by patch-reranker.py: active only when RERANKER_URL is set (env), so the
stock OpenAI behavior remains the default for anyone who does not opt in.
Installed to /app/mcp/src/ by the Dockerfile.
"""
from __future__ import annotations

import logging
import os

import httpx

from graphiti_core.cross_encoder.client import CrossEncoderClient

logger = logging.getLogger(__name__)


class InfinityRerankerClient(CrossEncoderClient):
    def __init__(self, base_url: str | None = None, model: str | None = None):
        self.base_url = (base_url or os.environ.get('RERANKER_URL', 'http://infinity:7997')).rstrip('/')
        self.model = model or os.environ.get('RERANKER_MODEL', 'BAAI/bge-reranker-v2-m3')

    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        if not passages:
            return []
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f'{self.base_url}/rerank',
                    json={'model': self.model, 'query': query,
                          'documents': passages, 'return_documents': False},
                )
                resp.raise_for_status()
                results = resp.json().get('results', [])
        except Exception as exc:  # noqa: BLE001 — degrade to unranked, never fail search
            logger.warning(f'infinity reranker unavailable ({exc}) — returning unranked passages')
            return [(p, 0.0) for p in passages]

        scored = [(passages[r['index']], float(r['relevance_score']))
                  for r in results if isinstance(r.get('index'), int) and r['index'] < len(passages)]
        seen = {p for p, _ in scored}
        scored += [(p, 0.0) for p in passages if p not in seen]
        return sorted(scored, key=lambda x: x[1], reverse=True)
