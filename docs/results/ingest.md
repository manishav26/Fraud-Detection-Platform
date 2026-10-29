# Ingest checks (week 2, step 3)

Local stack (`make up`, `make topics`), requests through Kong with a valid JWT.

| Check | Result |
|---|---|
| Good payment `t1` | 200, `decision: approve` (stub score 0.12) |
| Saved before deciding | `txn.raw` has key `u1` and the full payment JSON |
| Bad payment `t2` (amount -5) | 422, `Input should be greater than 0` at `body.amount` |
| Copy of the bad payment | `dlq.txn` has the path, the error, the original body and `received_at` |
| Redpanda stopped, payment `t3` (first try) | **504 from Kong after 60 s**: aiokafka retried "broker not ready" forever |
| Same, after adding a 5 s limit on each send | **503** `could not record the transaction; retry with the same txn_id` |
| Redpanda started again, retry `t3` | 200, `decision: approve`: ingest reconnects by itself |

What we learned: a client library's own timeout setting is not a guarantee. The 60 s hang would
have held one connection per payment open during an outage; now the client knows within 5 s and
retries. Bad bodies (422, copy in `dlq.txn`) and the outage path are also covered by
`tests/test_ingest.py`.
