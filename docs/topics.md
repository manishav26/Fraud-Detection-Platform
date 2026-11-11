# Redpanda topics

Created by `make topics` (script: `scripts/create_topics.sh`). Running it again is safe.

| Topic | Partitions | Message key | What goes in it | Who writes / reads |
|---|---|---|---|---|
| `txn.raw` | 6 | `user_id` (a pseudonym, `docs/privacy.md`) | Every valid payment, as received except the user id, which ingest pseudonymizes | ingest-svc writes; stream job reads |
| `txn.scored` | 6 | `user_id` | Payment plus score and decision | ingest-svc writes; alert-svc and monitoring read |
| `labels` | 3 | `txn_id` | Fraud answers, released 14 simulated days late | label simulator writes; drift job reads |
| `model.events` | 1, compacted | model name | Which model version is live | training/promotion writes; scoring-svc reads |
| `dlq.txn` | 1 | none | Messages that failed validation, with the error | ingest-svc writes; we inspect by hand |

A `txn.scored` message is the payment exactly as `txn.raw` has it, plus scoring's answer and a
time: `score`, `decision`, `model_version`, `reasons`, `cold_start`, `degraded`, `scored_at`.
Ingest publishes it after answering the client, in the background (`docs/ingest.md`).

Why these choices:

- **Key `user_id`:** Redpanda sends all messages with the same key to the same partition, and a
  partition keeps order. So one user's payments are always read in the order they happened, which
  the per-user history features need.
- **6 partitions** on the busy topics: up to 6 copies of a consumer can share the work. Fewer on
  quiet topics.
- **Compacted `model.events`:** Redpanda keeps only the latest message per key, so a restarting
  scoring-svc reads one message to learn the live model version, not the whole history.
- **Dead-letter queue `dlq.txn`:** bad input is set aside for inspection instead of crashing the
  pipeline or being silently dropped.
- **Replicas:** 1 on the single local broker. On a 3-broker cluster use `TOPIC_REPLICAS=3`, so
  each message is stored on 3 brokers and survives one broker going down.
