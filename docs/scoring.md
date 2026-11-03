# scoring-svc

Code: `services/scoring-svc/app/main.py`. Tests: `tests/test_scoring.py`.
Deploy config: `deploy/scoring.env`. Model: Manisha's `ml/model.py` (`FraudModel`).

## One payment

```
ingest-svc -> POST /score
  1. user known?   no (id has an "NA" part)        -> cold start, no history
                   yes -> Cassandra: earlier payments -> history_for_one -> 12 features
                          (no earlier payments: counts are 0, cold_start = true)
  2. FraudModel.predict_one(request, history) -> calibrated probability
  3. FraudModel.decide(probability) -> approve / review / decline
     review or decline -> FraudModel.explain_one -> top 3 reasons
  4. known user -> save the payment to Cassandra
  -> {txn_id, score, decision, model_version, reasons, cold_start}
```

## Why these choices

- **Same feature code as training:** history comes from `features/history.py`
  `history_for_one`, which Manisha's test proves gives the same values as training's
  `add_history`. The user id comes from the same `add_proxy_id` (replay).
- **Unknown users like training:** training gives no history to ids with a missing part
  (`uid_known = False`, written as `NA`), so scoring does the same and doesn't save them: many
  different people share an id like `9500|NA|98.0`.
- **One place decides:** `model.decide()` uses the thresholds saved inside the model (from
  `config/decision.yaml` at training time). ingest-svc no longer has its own copy (it had 0.30
  instead of 0.39).
- **Reasons only when flagged:** TreeSHAP takes about 3.3 ms (`docs/results/reasons.md`) and about
  98% of payments are approved, so most skip that cost. Only features that raised the risk are
  listed, so a payment can have fewer than 3 reasons.
- **A reason is an extra:** if explaining fails, the error is logged and the payment still gets
  its decision, with an empty list.
- **Save before answering:** the user's next payment must see this one. The replay sends one
  request per user at a time, so the next one starts only after this answer.
- **Cassandra slow or down:** the payment is still scored, without history, with `degraded=true`.
  Better a slightly weaker score than no answer or a late one (`store/guarded.py`):
  - **Time limits:** reads 150 ms, writes 500 ms, set in `deploy/scoring.env` from measurements
    (`docs/results/safe_reads.md`). Writes get longer because a skipped write leaves a gap in history,
    while a write that times out may still land, and writing it again is safe.
  - **Circuit breaker:** after 5 failures in a row, calls stop for 5 s, so payments don't each wait
    out the limit. Then one trial call goes through. Each pod has its own breaker.
  - **`degraded` is separate from `cold_start`:** "we couldn't get the history" isn't "this user is
    new". The log gets one line when the breaker opens and one when it closes, not one per payment.
  - **The model still decides.** Sending every degraded payment to review would flood analysts
    during an outage. The accepted risk: overloading Cassandra removes "too many payments this
    hour" signals, which may help a fraud burst (threat 7). A metric will show degraded payments.
  - **Skipped writes are gaps** in that user's history. A write that fails is logged with its
    `txn_id` (`skipped history write for ...`). While the breaker is open writes are only counted,
    and the total is logged when Cassandra returns. The payments are still in `txn.raw`, so a repair tool could fill them; not built for now.
- **Plain `def` endpoint:** the Cassandra driver and the model are blocking calls; FastAPI runs a
  plain `def` in a worker thread so other requests keep moving.

## Model integrity (threat 11, test ST9)

`FraudModel.load` checks the files against `manifest.json`, but that file sits next to the model:
anyone who can swap `model.txt` can also rewrite the manifest. So scoring-svc first checks both
files against SHA-256 values **pinned in `deploy/scoring.env`**:

| Situation | What happens |
|---|---|
| Files match the pins | model loads |
| A file was swapped (even with a matching rewritten manifest) | service refuses to start |
| Pin missing from the config | service refuses to start |
| No model folder at all (fresh clone, CI) | stub model, score 0.12, real thresholds |

Refusing to start is deliberate: falling back to the stub on a mismatch would let an attacker
switch fraud checks off just by corrupting the file. The model folder is mounted read-only.
Deploying a new model means changing the pins in a reviewed PR. Swapping models while running
("keeps serving the old model", ST9) comes with the model registry in week 3.

## Not done yet

- Clients can see score and reasons, which helps someone probing the API (threat 14). Before E6,
  roles limit clients to the decision and keep the rest for analysts.
- User ids are stored as sent; HMAC pseudonyms (threat 8, ST11) come later.
