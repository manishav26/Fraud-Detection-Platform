# Security test results

One row per test in `security/threat-model.md`. Each control needs a pass number, or it is only a claim.

| ID | Test | Result | Evidence |
|----|------|--------|----------|
| ST1 | Forged, expired, altered and `alg: none` tokens | **pass**: 1,000 of 1,000 got 401, none reached ingest (cluster) | below; `make k8s-st1` |
| ST2 | Each role calls every route | **pass**: all 16 calls match the roles table, compose and cluster | below; `make st2` |
| ST3 | Pod outside the mesh calls scoring directly | pending (week 4) | |
| ST4 | ingest tries to reach Postgres | pending (week 4) | |
| ST5 | Schemathesis fuzzing | pending (week 5) | |
| ST6 | Injection strings | pending (week 5) | |
| ST7 | One client floods against its limit | **limit: pass** (excess gets 429, held to ≤50/s). **Isolation: not shown on a laptop** (others' p95 +30% and +61% vs allowed traffic, within run-to-run noise); rerun on separate machines in week 5 | below; `make st7` |
| ST8 | Edit an audit row by hand | **pass**: verify names the edited row | `docs/results/alerts.md` |
| ST9 | Swap the model file | **pass**, compose and cluster | `docs/results/promotion.md` |
| ST10 | CI scan | pending (week 4) | |
| ST11 | Raw user ids in logs and stored rows | pending (week 5) | |
| ST12 | Replayed and old requests | pending (week 5) | |
| ST13 | Label writes by the wrong service account | pending (week 5) | |
| ST14 | Forced drift alarms | pending (week 5, with Manisha) | |

## ST1: bad tokens (k3d cluster, 4 Nov)

Each payment carried a bad token; `txn.raw` was counted before and after.

| Bad token | Sent | Answers |
|---|---:|---|
| forged (another private key) | 167 | 401 x 167 |
| expired (real key, an hour old) | 167 | 401 x 167 |
| altered (role changed to admin) | 167 | 401 x 167 |
| `alg: none` | 167 | 401 x 167 |
| algorithm confusion (HMAC with the public key) | 166 | 401 x 166 |
| unknown client (real key, made-up `iss`) | 166 | 401 x 166 |

All 1,000 refused with 401. `txn.raw`: 31,723 messages before and after, so none reached ingest.

## ST2: roles (compose and cluster, 4 Nov; identical)

| Route | no token | client | analyst | admin |
|---|---|---|---|---|
| Send a payment | 401 | 200 | 403 | 403 |
| List cases | 401 | 403 | 200 | 200 |
| Read a case | 401 | 403 | 404 | 404 |
| Resolve a case | 401 | 403 | 404 | 403 |

404 means allowed: the request passed Kong and alert-svc's role checks and asked for a case that
doesn't exist, so the test resolves nothing real. 403 came from Kong.

## ST7: rate limit

**First run, as originally written** (merchant-2's p95 alone vs during merchant-1's 200/s flood):

| | Status codes | p95 ms |
|---|---|---|
| merchant-2 alone, 10/s | 200 x 200 | 47.7 |
| merchant-1 flooding, 200/s | 200 x 2,553, 429 x 1,447 | |
| merchant-2 during the flood | 200 x 200 | 395.7 (**+729%**) |

The limit worked (merchant-1 accepted 47/s against 50/s), but merchant-2 slowed sharply. The limit
correctly lets 50/s through, and those are real payments: the platform went from about 10 to 60
payments per second, which this laptop can't take without slowing down. No rate limit can prevent
that, so this comparison can't show whether the limit protects others.

**The test now compares the flood with merchant-1 staying just under its limit** (45/s), each twice:
what others feel from the excess, not from allowed traffic. The pass condition was updated to match
in the threat model.

**Two runs of the new test** (compose, k3d off), merchant-2's p95 in ms:

| Run | A: alone | B: merchant-1 at 45/s | C: flood | B again | C again | B avg | C avg | C vs B |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 47.5 | 112.1 | 91.9 | 55.1 | 126.4 | 83.6 | 109.1 | +30.5% |
| 2 | 39.1 | 46.0 | 91.8 | 57.3 | 74.8 | 51.7 | 83.3 | +61.2% |

In every flood run merchant-1 was held to 41-48/s with 1,109-1,388 requests getting 429, and every
merchant-2 payment was answered.

- **The limit works:** the flood's excess was refused, and what it costs others fell from +729%
  (vs nothing) to +30-61% (vs allowed traffic).
- **The 10% rule can't be judged on this laptop:** two identical B runs differed by up to 2.4 times
  (46 vs 112 ms), far more than 10%. Both runs still put C above B, so rejecting excess probably
  isn't free here: Kong, the rejected requests and the flood's own sending all share one CPU.
- **The flood didn't reach 200/s:** each C run took about 57 s instead of 20 s, because slow
  accepted requests held the sender's slots, so it actually offered about 70/s. A real test needs
  the flood sent from another machine.
- **Next:** rerun in week 5 with the load sent from a separate machine (the rented VM planned for
  the Cassandra tests), and more repeats. Until then ST7's isolation half is open, not passed.
