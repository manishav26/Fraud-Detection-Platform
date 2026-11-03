# ingest-svc

Code: `services/ingest-svc/app/main.py`. Tests: `tests/test_ingest.py`.

## What happens to one request

```
client -> Kong (JWT check) -> ingest-svc
   1. body matches the contract?   no  -> copy to dlq.txn, answer 422
   2. save to txn.raw              fails -> answer 503 (client retries)
   3. ask scoring-svc for score -> answer 200 with the decision
   4. in the background: payment + decision -> txn.scored (alert-svc opens cases from it)
```

**Why step 4 runs after answering:** the decision is made and the payment is already safe in
`txn.raw`. Waiting would only slow every payment, by up to 5 s if Redpanda struggles. If the publish
fails, the client still has its answer, but a flagged payment could miss its case. Each failure is
logged with its `txn_id` and counted (`scored_publish_failures`, a metric from week 3, step 7). At
shutdown, ingest waits for publishes still running before closing its producer.

The client always gets its decision in the same request (the contract we agreed: 200 + decision).

## Why these choices

- **Save before scoring:** every payment we decide on is recorded first. If Redpanda is down we
  answer 503 rather than make a decision with no record of it (needed for audit and for the
  history features later).
- **`acks=all`:** Redpanda confirms only after every copy of the partition has the message. With 3
  brokers and 3 replicas, losing one broker loses no payments. Locally there is one copy.
- **Idempotent producer:** if a network blip makes the producer resend, Redpanda drops the
  duplicate. A client retry after a 503 is safe too: the txn_id is the same, and the Cassandra
  history write (step 5) is an upsert on it.
- **Key `user_id`:** same user -> same partition -> payments read in order (docs/topics.md).
- **5 s timeout:** a broker that is down gives the client a 503 after 5 s instead of a hang.
  aiokafka's own timeout does not cover this (it retries "broker not ready" forever, and Kong
  answered 504 after 60 s in our first test), so ingest sets the limit itself with
  `asyncio.wait_for`. A timed-out message may still be delivered once Redpanda is back, so after
  a client retry `txn.raw` can hold the same `txn_id` twice; readers treat `txn_id` as the
  unique id (the Cassandra write is an upsert on it).
- **Dead-letter queue:** a rejected body is kept in `dlq.txn` with the error, so we can see what
  bad input arrives without crashing anything. Only requests that passed the JWT check reach
  ingest, so strangers can't fill the queue; bodies are cut at 10,000 characters.
- **Stricter checks:** `ts` must include a timezone (`...Z` or `+02:00`), and `txn_id` / `user_id`
  must be 1-128 characters. Both were already implied by the contract.

## Redpanda addresses

Redpanda tells each client which address to use next. One address can't work for both the laptop
and other containers, so there are two listeners:

| Listener | Address | Used by |
|---|---|---|
| internal | `redpanda:29092` | containers (ingest-svc, later the stream job) |
| external | `localhost:9092` | your laptop (tools, scripts) |
