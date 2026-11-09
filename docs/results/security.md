# Security test results

One row per test in `security/threat-model.md`. Each control needs a pass number, or it is only a claim.

| ID | Test | Result | Evidence |
|----|------|--------|----------|
| ST1 | Forged, expired, altered and `alg: none` tokens | **pass**: 1,000 of 1,000 got 401, none reached ingest (cluster) | below; `make k8s-st1` |
| ST2 | Each role calls every route | **pass**: all 32 calls (8 routes, including the week 4 model routes) match the roles table, compose and cluster | below; `make st2`; `docs/results/model_admin.md` |
| ST3 | A pod outside the mesh, allowed by the network rules, calls scoring's `POST /score` | **pass** (cluster): refused with 403; the same call from a real, meshed ingest pod answered 200 | below; `make k8s-st3`; `docs/mtls.md` |
| ST4 | Every workload and a pod with no labels try every pod and port | **pass** (cluster): only the 23 allowed paths connect; the intruder pod reaches nothing. Before the policies, every pod reached every port. Cassandra logins: scoring may only read and write its one table (10 of 10 checks) | below; `make k8s-st4`; `docs/network.md` |
| ST5 | Schemathesis fuzzing | pending (week 5) | |
| ST6 | Injection strings | pending (week 5) | |
| ST7 | One client floods against its limit | **limit: pass** (excess gets 429, held to ≤50/s). **Isolation: not shown on a laptop** (others' p95 +30% and +61% vs allowed traffic, within run-to-run noise); rerun on separate machines in week 5 | below; `make st7` |
| ST8 | Edit an audit row by hand | **pass**: verify names the edited row | `docs/results/alerts.md` |
| ST9 | Swap the model file | **pass**, compose and cluster | `docs/results/promotion.md` |
| ST10 | CI scan | **pass** (laptop, 9 Nov; CI after push): no secrets in 80 commits, no known vulnerabilities in our pins, no unwaived HIGH/CRITICAL in config or images. First run found 4 HIGH CVEs in every image and 4 HIGH settings; fixed or waived with reasons | `docs/supply-chain.md`; `make scan` |
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

## ST4: network isolation (k3d cluster, 8 Nov)

Each source pod tries a TCP connection to another pod of every workload, on every port it listens
on (15 targets). The "intruder" is a pod with no labels, standing in for a compromised pod.

**Before the policies** (default Kubernetes: everything open): every source opened every port.
For example token-issuer, which needs nothing, opened Cassandra, Postgres, Redpanda and RustFS;
the intruder opened all 15, every database included.

**After** (`deploy/k8s/base/network.yaml`):

| From | Could open | Blocked |
|---|---|---|
| ingest-svc | scoring-svc:8000, redpanda:29092 | 13 of 15 |
| scoring-svc | mlflow:5000, redpanda:29092, cassandra:9042 | 12 of 15 |
| alert-svc | redpanda:29092, postgres:5432 | 13 of 15 |
| model-admin-svc | redpanda:29092, postgres:5432 | 12 of 14 |
| token-issuer | nothing | 14 of 14 |
| mlflow | postgres:5432, objectstore:9000 | 12 of 14 |
| intruder (no labels) | nothing | 15 of 15 |

Exactly the paths in `security/tests/network_paths.py`. A copy can't reach its twin (scoring to
the other scoring pod: blocked). 14 targets where a workload has one copy: a pod isn't tested
against itself. Afterwards ST2 still passed (all 32 calls through Kong) and a replay of 300
payments got 300 answers, so no allowed path was missed.

Two test details found on the first run: k3s's enforcer **rejects** a blocked connection at once
rather than dropping it, so a refusal counts as blocked (every port tried is listening); and a pod
connecting to its own address never leaves the pod, so it is never a target for itself.

### ST4, database logins (k3d cluster, 8 Nov; `make k8s-st4-db`)

Until this step Cassandra had no login at all. Now:

| Login | Statement | Expected | Got |
|---|---|---|---|
| scoring | `SELECT txn_id FROM fraud.txn_by_user LIMIT 1` | ok | ok |
| scoring | `SELECT role FROM system_auth.roles` | refused | refused |
| scoring | `CREATE TABLE fraud.st4_probe ...` | refused | refused |
| scoring | `DROP TABLE fraud.txn_by_user` | refused | refused |
| scoring | `CREATE KEYSPACE st4_probe ...` | refused | refused |
| scoring | `CREATE ROLE st4_probe ...` | refused | refused |
| scoring | `GRANT SELECT ON ALL KEYSPACES TO scoring` | refused | refused |
| scoring, wrong password | `SELECT ...` | refused | refused |
| cassandra (built-in, known password) | `SELECT ...` | refused | refused |
| fraud_admin | `SELECT ...` | ok | ok |

Afterwards a replay of 600 payments got 600 answers with 0 scored without history (scoring logs
in as `scoring`), and `make k8s-read-timing` worked as `fraud_admin` through its tunnel (p95
6-8 ms). Limit: Cassandra's MODIFY also allows DELETE and TRUNCATE on that table
(`docs/cassandra.md`).

## ST3: mTLS with Linkerd (k3d cluster, 8 Nov)

`linkerd check --proxy -n fraud`: all checks pass, every pod in the namespace has a proxy.

| Caller | Network rules | Mesh identity | `POST /score` to a scoring pod |
|---|---|---|---|
| test pod labelled ingest, outside the mesh | allowed | none | **403** |
| real ingest pod | allowed | ingest-svc | 200 |

The network rules let the test pod's packets through (it carries ingest's label); the mesh
refused it because it has no identity. Two layers, each tested on its own.

**ST4 again, with the mesh.** The first run with the mesh failed: ingest, scoring, alert-svc and
model-admin-svc could reach Redpanda's metrics port 9644. Between meshed pods every connection
arrives on the proxy port 4143, so the NetworkPolicy can no longer tell 29092 from 9644. The fix
moved per-port rules into the mesh (`deploy/k8s/base/mesh-ports.yaml`: Redpanda 29092 for our
four services, 9644 and Kong 8100 for Prometheus only, checked by identity). After it, ST4 passed
with the same table as before the mesh, the database logins (10 of 10) and ST2 (32 of 32) still
passed, and a replay of 600 payments got 600 answers.

ST4's check had to change for the mesh: a meshed pod's own proxy accepts every outgoing
connection at once, so ST4 now counts a path as open only on a real answer (`docs/mtls.md`). The
mesh's refusal is a 403 with an empty body; an app's 403 (RustFS to an unsigned request) has one.

**What the mesh costs** (30/s through Kong, 1 minute, rows 110,800 on, never sent before):

| End to end, 30/s | p50 | p95 | p99 | Scored without history |
|---|---|---|---|---|
| baseline, no mesh (3 runs of 10 min, `docs/results/baseline.md`) | 20 ms | 188 ms (103-334) | 905 ms (653-1,326) | 0.2% |
| with the mesh (1 run of 1 min) | 28 ms | 192 ms | 1,746 ms | 0.06% (1 of 1,800) |

- About **+8 ms at the middle (p50)**: encrypting and checking each hop (Kong to ingest, ingest to
  scoring, scoring to Cassandra and Redpanda).
- **p95 stays inside the baseline's range.** p99 isn't comparable: a minute holds about 18
  payments above it, so one laptop stall decides it.
- Server side, scoring's Cassandra calls look slower: p95 read 22 ms (baseline 8-15), write
  19 ms (5-10), as each call now passes two proxies. That window also held a bursty replay of
  600 payments just before, so these are indicative only.
- A proper comparison (10 minutes, mesh against no mesh, same rows) belongs with week 5's ramp
  test.

