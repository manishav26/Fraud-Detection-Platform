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

## Replays (threat 6, ST12)

A request captured on the way in could be sent again. Two controls:

- **A 5-minute window on the token.** Kong checks a token's signature and expiry (tokens live 15
  minutes). For payments, ingest also reads the token's signed issue time (`iat`) and refuses a
  token issued more than 5 minutes ago (30 s allowed for clock drift): **401 "token too old for a
  payment"**, before anything is stored. A missing or unreadable token gets 401 too. Our sender
  takes a new token every 4 minutes, so it never meets the limit. Other routes keep the 15 minutes.
- **Inside the window, `txn_id` is the idempotency key.** The same payment again gets the same
  answer and changes nothing: Cassandra's history row is an upsert on (user, time, txn_id), a
  case is unique per txn_id, and the feature job skips a txn_id it has seen. `txn.raw` and
  `txn.scored` keep both copies, because they are logs; every reader of them is idempotent.

`make k8s-st12` checks both: the same flagged payment twice, and tokens issued 10 and 1 minutes ago.

## Input (threat 5, ST5 and ST6)

**One contract, checked at the edge and again in scoring.** `features/schema.yaml` lists the 51
attrs and, since week 5, their types: 15 are text (the model's categorical features), the other
36 numbers. `features/contract.py` turns that into one strict model, used by ingest and scoring:

| Input | Answer |
|---|---|
| a number attr as text (`"D15": "AAA"`), as true/false, or as NaN / Infinity | 422 |
| a text attr as a number, or over 256 characters (the data's longest is 16) | 422 |
| an attr that isn't one of the 51 | 422 |
| `txn_id` / `user_id` with anything but letters, digits and `. _ : \| -` | 422 |
| `channel` over 256 characters | 422 |
| `ts` without a time zone, with an offset outside -12:00 to +14:00, or outside 1970-2099 (UTC) | 422 |

Every 422 keeps a copy in `dlq.txn` and stores nothing else. **ingest stores `ts` in UTC**
(`...Z`), so every reader (Spark, the Python writer, Cassandra, cases) gets one form and never an
offset. Why the time rule: ST5 sent `0387-08-26T14:46:05-21:11`; Python accepts offsets up to
23:59, so ingest and scoring took it, but Java allows 18:00 at most, and the Spark feature job's
strict cast failed on it. One bad row failed the batch, and every restart retried the same batch
from its checkpoint: a crash loop that stopped all feature computation. **Why:** ST5's fuzzer sent text in a
number attr; ingest and scoring both took any text or number for any attr, and the model's encoder
crashed on `float('AAA')`, a 500. The attr rules lived in three places that disagreed (the
contract had names only; the OpenAPI file had types; the services had neither). Tests now check
that the OpenAPI file, the model's categorical features and all three data splits agree with
`features/schema.yaml`, so a retrain or a doc edit can't drift from it.

**422s never echo the input** (all services, `observability/http.py`): each problem is listed by
where it is and what it is. FastAPI's own answer repeats every bad value, which reflects the
caller's input, and failed with a 500 on `NaN` (Python's JSON reader takes it, but it can't be
written back out); found while testing the contract.

**Free text stays free** (channel, the text attrs, a case note) within those limits, and is stored
exactly as sent: every query takes values as parameters (prepared CQL, parameterized SQL). Notes
can't hold a NUL character, which Postgres text can't store (422; found while writing ST6).

**When scoring fails after the payment is recorded** (an error answer, no answer in time, a broken
connection after the one retry, or an answer that isn't a decision): **503 "recorded but not
scored: retry with the same txn_id"**, the contract's meaning of 503, since `txn_id` makes the
retry harmless. It is logged and counted (`ingest_scoring_failures_total{kind}`); nothing goes to
`txn.scored` until the retry. Before, it was a bare 500.

`make k8s-st6` sends injection strings into every text field; `make k8s-st5` fuzzes the OpenAPI
contract with Schemathesis.
