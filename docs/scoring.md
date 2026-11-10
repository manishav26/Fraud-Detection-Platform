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

## Probing the API: E6 defenses D1 and D3 (threat 14, week 4 step 7c)

Someone with a stolen card can resend a payment the model flags, changing what they control
(amount, e-mail, device, time), until a variant is approved. Manisha's E6 (`docs/results/e6.md`)
simulated this: with no defense, 50.2% of flagged frauds got through within 50 tries. D1 + D3
cut that to 17.5%, with 0.001 points more false positives. Both are built here exactly as she
simulated them (she confirmed the details, 9 Nov).

**D1: clients see only the decision.** A client gets `{txn_id, decision}`. ingest-svc picks the
answer from the role Kong passes in `X-Consumer-Groups` (Kong sets that header itself and
replaces any the caller sent). Only the test-only `tester` role (the `load-test` client) gets
the full answer, so replay can keep reporting scores, cold starts and degraded answers.
**Anything else gets the short answer**, including a missing role, so a Kong mistake can't leak
the score. Nothing changes inside: `txn.scored`, cases and the SIEM keep every field.

**D3: probe detection per card** (`store/probe.py`):

| Rule | Detail | Same as E6 (`probe_frozen`) |
|---|---|---|
| Card | the proxy user id (card1 + addr1 + first-seen day) | yes |
| What counts | the **model's own** flags (review or decline). A payment D3 sends to review is saved as not flagged, so D3 never feeds itself | yes |
| Window | **rolling** 24 h before the payment's own time (`ts`); a flag exactly 24 h old still counts. A card un-freezes as old flags age out | yes (a test checks both on random cards) |
| When | 5 or more model flags before it: so the **6th payment after 5 flags** is the first one held | yes |
| Action | an approval becomes a **review**; a decline stays a decline | yes |
| Unknown cards (NA in the id) | not checked: they have no history | yes |
| Cassandra down (degraded) | not checked: no history to count | (not simulated) |

**How it counts:** `txn_by_user` has a `flagged` column. Scoring already reads the card's whole
history for its features, so D3 counts in that same read: no extra query. Rows written before
step 7c, and by the backfill, have no value and count as not flagged.

**Evidence:** the reason `probe: 5 or more flagged payments on this card in 24 h` comes first in
`reasons`; `probe: true` in the answer (tester), in `txn.scored`, in the case's audit row and so
in the SIEM event; and the metric `scoring_probe_holds_total`. Replay prints how many it held.

**Switch:** `PROBE_DETECTION=on` in `deploy/scoring.env`. `make k8s-probe-attack D3=off` turns it
off for one run to show what it stops, and back on at the end.

**Test, live** (`make k8s-probe-attack`, `load/probe_attack.py`): E6's attacker against the real
API, as merchant-1. For each of 5 fraud cards the model flags, 30 variants on the same card, 40
minutes apart, each changing one field it controls and keeping changes that improve the answer.
Pass: once a card has had 5 flagged answers, no later variant is approved, and every answer to
merchant-1 has only `txn_id` and `decision` (also when it sends `X-Consumer-Groups: tester`
itself). With `D3=off` the same attack shows approvals after the 5 flags, which D3 stops.
False positives aren't measured live: E6 measured them over 90 days of real payments.

**Result** (`docs/results/e6_live.md`): with D3 on, 0 approvals after 5 flags (45 with it off),
and clients never saw more than the decision. But the attack beats D3 before the 5th flag on
about half the cards, not E6's 17.5%: the attacker's own tries become the card's history, so its
e-mail and device stop looking new. To agree with Manisha.

**Limits:** a probe spread across many cards isn't caught (E6 says the same); D3 can't run while
Cassandra is down; unknown cards aren't checked, which an attacker could use; a probe that wins
within its first 4 tries isn't stopped (see the result above).

## Not done yet

- User ids are stored as sent; HMAC pseudonyms (threat 8, ST11) come later.
