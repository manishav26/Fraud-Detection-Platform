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
- **Save before answering:** the user's next payment must see this one. The replay sends one
  request per user at a time, so the next one starts only after this answer.
- **Cassandra down:** the payment is still scored, as cold start, and the error is logged.
  Better a slightly weaker score than no answer.
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

- `reasons` stays empty until week 3.
- User ids are stored as sent; HMAC pseudonyms (threat 8, ST11) come later.
