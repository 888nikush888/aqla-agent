# Token efficiency

The main model, reasoning effort, routing fallback and financial authority remain unchanged. Work is still durable in trading SQLite; history compression is never trading authority. Protection/reconciliation runs independently of the model.

## Model-facing reads

- `status(view="summary")`: current ready/blocked/recovery counts and at most eight IDs, notification count, pause/Setup, account readiness/recovery and journal state. Inventory is explicitly not refreshed. Full and compact views remain compatible.
- `source_context(view="decision")`: lossless original source bodies keyed by event ID, one anchor reference, relevant profiles/pipelines, and owned trade decision facts. Full stored trades are linked through diagnostics.
- `source_context_batch(event_ids)`: 1–8 distinct anchors, shared messages/trades/configurations, `message_ids`/`trade_ids`/`configuration_id` per item, and an independent receipt per anchor. Never truncates original text.
- `notifications_read(view="rendered"|"facts")`: existing complete rendered messages or original structured facts, not both. IDs, original versions, cursors and ACK semantics are unchanged. A summary-only overflow response explicitly exposes `details_required`; complete versioned details must be read before reporting/ACK.
- `trade_diagnostics(sections=[...])`: plan, sources, orders, fills, accounting, operations, lifecycle or prices. Original/later source bodies occur once in `source_messages`; attribution refers to revision IDs. Missing originals remain unknown.
- `journal_read(columns=[...], start=..., end=..., time_column=...)`: aware interval `[start,end)`, existing UTC column (first UTC column by default), filters and bounded scan cursor. Projection follows verification of the complete original row, including omitted fields. ID/verification remain visible; invalid rows have no original fingerprint. Statistics and monthly evidence remain preferred for aggregates.
- `aqla_read_tool_result(output_id, sections=[...], record_ids=[...])`: selects named JSON sections or up to eight IDs in one section of the existing immutable session-owned output. Continue character paging with exactly the same selection. EOF means the selection is complete, not the omitted original. Original hash/ownership/expiry and the lossless reader remain available. No paths or arbitrary execution.

## Classifications

`interpret_source(..., result_view="ack")` returns identity, status, changed flag and a full-context detail reference, without repeating original source/evidence. Default full output is unchanged.

`interpret_source_batch(items)` accepts only report/unrelated/incomplete/ambiguous. Every item has its own current signed receipt, original quote/reference and reason; authentication is rechecked per item. Distinct IDs only, maximum eight. Partial success is explicit; stale or failed items require their own reread. There are no trading fields, plan creation or exchange actions. Existing execution APIs are unchanged.

Decision reads issue compact `sr2` receipts: bounded compressed claims signed with a separate HMAC domain. Legacy JSON receipts are still verified. Anchor/context binding, 900-second expiry, recovery immutability and constant-time signature comparisons are retained.

## Native runtime

Wake scheduling admits at most one pending wake; new work is read from authoritative state at execution time. Unchanged work retries after 30, 60, 120, then 300 seconds. New work, progress or changed notification/reader/recovery signatures reset the interval. An accepted but unconsumed wake is retried after 300 seconds only when no native model turn is running. Reconnect/unload clears admission state. Gateway scheduling acceptance is never treated as completed work.

Source turns use `aqla-source`, a fixed subset of the existing MCP tools. Operator tools/Sheets/skill mutation schemas are excluded; independent authorization hooks remain active. Operator turns retain their configured toolsets. Different toolset keys preserve native cache isolation.

New installations disable automatic background review and the skill iteration trigger. Native manual skill proposals/approval remain available. Tool search stays automatic with listing off. Compression candidates: 64,000-token absolute threshold, deterministic archived pruning at 48,000, 8,000-character eligibility, 4,096-token minimum reclaim, 20 protected recent messages, lean tail and micro-compaction off. No lowered model context length or output-token cap.

Native compression also keeps large Aqla/Sheets tool bodies in immutable, session-owned output receipts before replacing their model-history copies. Active tail and canonical tool arguments remain unchanged; source turns can recover evidence without operator-only history search. If private receipt storage fails, the original body is retained.

Runtime toggles under `plugins.entries.aqla-events.settings.token_efficiency`: `wake_coalescing`, `source_toolsets`, `usage_metrics` (all default true). Each can be rolled back separately. Compression/review settings are independent; API defaults remain full.

## Measurement

Private `hermes-home/cache/aqla-usage.sqlite3` keeps aggregate runs for 90 days: main/compression responses, provider-reported uncached/cache/output/reasoning tokens, availability, latency, tool call/error/repeated-result counts and non-actionable classification count. It contains no messages, tool arguments, credentials or source text. Reasoning is a subset of output and must not be added again. Failed/missing usage is explicit; auxiliary summary calls are included. Measurement failure cannot stop trading.

`scripts/benchmark_token_efficiency.py --output ...` compares actual MCP serialization using synthetic data. Results measure UTF-8 bytes, not provider token savings or a daily bill. Optional isolated native same-model replay uses a private key environment variable, no tools/real orders/session database and only seeded test cases. External RTK/Context Mode/LLMLingua are not installed in the trading profile; the native changes should be measured before adding another integration.
