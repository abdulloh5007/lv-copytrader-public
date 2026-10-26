# Channel brief

The copytrader pool and the channel reviewer are different tasks.

## Signal workers

`INGEST_WORKERS` defaults to 3 and cannot be set above 3. Each source has one bounded
queue. A worker blocked in triage or in the tool loop does not stop the other two from
taking the next queued message. Reply chains still share one lock (a ladder reply waits
for its teaser). Broker writes stay one at a time.

Verified in `tests/test_worker_pool.py`: three posts enter the worker body together, and
a fourth stays queued until one of them finishes.

## Reviewer

`channel_reviewer` is a fourth task. It never acquires the agent-run semaphore.

Once per active source it writes `DATA_DIR/channel-briefs/active/brief.md` (at most 60
lines): which shapes are signals, which are celebration or noise, how a teaser differs
from the ladder reply, the usual stop and targets, and channel quirks.

- Stored messages are the sample.
- A thin sample (fewer than 12 posts, or a span under two days) is filled from the last
  3–4 days of the Telegram channel via `TelegramService.fetch_recent_days`. Those
  messages are not ingested.
- Changing `chat_id` moves the previous brief, `meta.json`, and `study.json` into
  `channel-briefs/backups/`. Copytrader code may call `load_active_brief` only. That
  function opens `active/brief.md` and raises if the path resolves under `backups/` or
  `reviewer/`.
- While the study runs, triage keeps the previous prompt. When the brief is ready,
  triage appends it. The full agent prompt is unchanged.
- There is no schedule. The same channel is not studied again. Three failed model calls
  stop until the channel changes. Waiting for Telegram or for a provider does not spend
  an attempt.

The model is the one saved in Settings (OpenAI-compatible / ZenMux, or Anthropic if that
is what is configured). Nothing here assumes a specific gateway.

The `upgraded` branch is a parked change. Do not deploy it to a VPS from this work.
