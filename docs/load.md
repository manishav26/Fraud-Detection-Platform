# Replay and load tools

Code: `load/`. Tests: `tests/test_load.py`.

| Tool | Command | Sends | Used for |
|---|---|---|---|
| Replay | `make replay SPEED=1000 LIMIT=5000` | real IEEE-CIS payments (days 110-182), in time order | drift experiments, end-to-end checks |
| Synthetic | `make load RPS=100 SECONDS=60` | random payments at a fixed rate | load and latency tests, no data needed |

Both go through Kong with a real JWT, exactly like a client would.

## Replay: row -> request

| Request field | From |
|---|---|
| `txn_id` | `TransactionID` |
| `user_id` | proxy user id v1 (`features/proxy_id.py`, the same function training uses) |
| `amount` | `TransactionAmt` |
| `ts` | 2017-12-01 00:00 UTC + `TransactionDT` seconds |
| `channel` | `DeviceType` |
| `attrs` | the 51 columns in `features/schema.yaml` (read from that file); missing -> `null` |

**Why midnight UTC:** the model's `hour` feature is `(TransactionDT // 3600) % 24` in training and
the UTC hour of `ts` live. Starting at midnight UTC makes the two the same.

**Speed:** `SPEED=1000` plays 1000 seconds of data per real second: the 73 stream days take about
1.75 hours, about 33 payments per second on average (busier at peak hours). `SPEED=0` sends as
fast as the platform answers. `LIMIT` replays only the first N payments.

Results per payment (status, decision, score, latency, true fraud label) go to
`data/replay/results.csv`, not committed.

## How sending works (`load/sender.py`)

- **Open loop:** each payment is sent at its scheduled time, whether or not earlier answers have
  arrived. A slow platform then shows up as higher latency (and "furthest behind schedule"),
  instead of quietly slowing the test down and hiding the problem.
- **Up to `--concurrency` requests in flight** (32 for replay, 64 for synthetic).
- **One request per user at a time:** a user's next payment waits until the previous one is
  answered, so payments reach ingest in the order they happened (history features need this).
- **503 -> retry** up to 3 times (0.5, 1, 2 s) with the same `txn_id`.
- **401 -> new token, once:** tokens last 1 hour and a full replay takes longer.
- **Summary:** payments per second, status codes, decisions, retries, latency p50/p95/p99.
