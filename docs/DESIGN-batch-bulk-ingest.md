# Design: bulk ingest via the Message Batches API

**Status: accepted direction (2026-07-14), not yet implemented.**
Decision: the BULK route (onboarding / mass import) will run its LLM calls through
the Anthropic **Message Batches API** (50% token discount, all Messages features
including structured outputs). The DAILY route (`add_memory` → durable queue) stays
on the live API — there, latency matters and per-episode cost is negligible.

## Why

Mass ingest is the only place this stack spends real LLM money: hundreds of chunks
× several extraction/dedup calls each. Batches halve that bill with no quality
trade-off — batch requests support the same schema-constrained structured outputs
the pipeline depends on. A full ~700-chunk onboarding on Haiku drops from roughly
$25–45 to $12–20. Latency is the price (batches typically complete well within an
hour) and is irrelevant for onboarding, which is an offline, supervised operation.

Alternatives considered and rejected:
- **Subscription-routed extraction** (headless Claude Code / OAuth tokens): ToS-grey
  for exactly the mass use case that would justify it, loses server-side
  schema-constrained decoding (this pipeline's "no JSON fabrication" rule depends on
  it), and couples an OSS tool to a consumer product's auth. Not portable.
- **Cheaper live models only**: already done (the extraction profile is swappable);
  batching composes with it rather than competing.

## Architecture: a batching LLM client (gateway pattern)

graphiti-core's `add_episode_bulk` fires many independent LLM calls per phase
(extraction, node dedup, edge extraction, …) through its `LLMClient`. We do not
fork that pipeline; we swap the client:

```
graphiti add_episode_bulk
   └─ AnthropicBatchingClient (same interface as the patched AnthropicClient)
        ├─ collector: awaiting calls park a Future keyed by custom_id (uuid)
        ├─ flusher:   submits pending requests as ONE batches.create() when
        │             N pending ≥ FLUSH_SIZE or T ≥ FLUSH_WINDOW seconds
        ├─ poller:    awaits processing_status == "ended", streams results
        └─ resolver:  matches results by custom_id; succeeded → resolve future,
                      errored/expired/canceled → raise into the caller
```

Key properties:

- **Same call surface.** Callers (graphiti internals) `await` as if it were a live
  call; they never know a batch happened. All existing behavior — bounded request
  timeout, schema-constrained output, failure classification — is preserved or
  mirrored in how batch request params are built.
- **Structured outputs preserved.** Batch request params carry the identical
  `output_config.format` JSON schema the live path uses. Responses are validated
  client-side on retrieval; a validation failure raises, so the episode flows to the
  dead-letter exactly like a live-path failure. No JSON repair, ever.
- **Failure semantics unchanged.** An `errored`/`expired` batch item raises into the
  awaiting caller → bulk_ingest's existing no-retry rule applies (a batch that
  fails is dead-lettered whole; `add_episode_bulk` can partially land episodes, so
  in-place retry is forbidden) → serial dead-letter replay stays the recovery path.
- **Opt-in.** `memctl ingest --batch-api` (or `BULK_LLM_MODE=batch` in the
  container) activates the batching client; default remains the live API. The
  serial route (`memctl enqueue`) is untouched — it is the robustness route, and
  per-episode batching would add latency for nothing.

## Design considerations (to resolve at implementation time)

1. **Aggregation width vs DB pressure.** Good batch amortization wants many LLM
   calls in flight per flush, i.e. a high `max_coroutines`. But graphiti's semaphore
   also governs FalkorDB dedup scans, which saturate a small host at high
   concurrency (observed: 12 concurrent scans → sustained query timeouts; 4 is the
   safe ceiling on a NAS-class host). The batching client therefore needs the LLM
   wait to *not* occupy DB capacity: either a two-tier semaphore (wide for LLM-bound
   awaits, narrow for DB-bound work) or accepting per-flush widths equal to the
   safe semaphore and amortizing across flushes instead.
2. **Flush tuning.** `FLUSH_SIZE`/`FLUSH_WINDOW` trade batch efficiency against
   pipeline stalls between phases (a phase cannot finish until its last flush
   returns). Start conservative (e.g. flush every 60s or 100 pending) and measure.
3. **Timeout semantics.** The live path bounds each request (`LLM_REQUEST_TIMEOUT`).
   Batches need a wall-clock bound per flush (batch-level deadline, e.g. 2×
   expected turnaround) after which pending futures raise as timeouts →
   classification `timeout` → dead-letter. Never wait indefinitely on a batch.
4. **Idempotency across crashes.** A crash between submit and resolve leaves a
   completed batch unread. Persist submitted batch IDs (Redis, alongside the other
   `aimem:*` state) so a restarted run can re-attach instead of re-submitting; batch
   results remain retrievable for 29 days.
5. **Provider-agnosticism.** This stack is provider-agnostic by design. The
   batching client is an Anthropic-specific optimization behind a flag; other
   providers fall back to the live path. Keep the gateway interface generic so an
   OpenAI Batch equivalent can slot in later.

## Sequencing

Implement **after** the graphiti-core fixes plan (escaping backport, empty-graphname
guard, edge-fulltext re-MATCH fix) lands: those bugs sit in the same extraction/dedup
path this client wraps, and validating a new LLM transport on top of known-broken
queries would conflate failure modes. Validation then follows the house pattern:
canary group first (small JSONL, verify counts == distinct, dead-letter empty,
spend halved on the billing page), then it becomes the recommended onboarding mode
in the README.
