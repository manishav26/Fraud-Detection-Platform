# Threat model

Source: architecture plan, "Threat model and controls" (STRIDE plus four ML threats).
Attacker column is a short plain description added for the report. Test IDs ST1 to ST14 and E6 are
defined in the plan; each needs a pass number saved in docs/results/security.md.

| # | Threat | Asset | Attacker | Control | Test ID |
|---|--------|-------|----------|---------|---------|
| 1 | Stolen or forged token | gateway | Outsider with a stolen or crafted JWT | Short-lived JWT (15 min), RS256, scopes, key rotation, reject alg: none | ST1 |
| 2 | Wrong role does a privileged action | all APIs | Insider or client using the wrong role | RBAC: client, analyst, admin; model promotion is admin only | ST2 |
| 3 | Traffic read or changed between services | cluster | Attacker inside the network | mTLS everywhere (Linkerd, cert-manager) | ST3 |
| 4 | A compromised pod reaches the databases | cluster | Attacker who owns one pod | Default-deny NetworkPolicy, one allow rule per path, separate service accounts, least-privilege DB users | ST4 |
| 5 | Malformed or malicious input | ingest, scoring | Malicious client | Strict Pydantic schemas, size limits, prepared CQL and parameterized SQL, bad messages to dlq.txn | ST5, ST6 |
| 6 | Replay of a captured request | ingest | Outsider replaying a captured request | 5 min timestamp window; txn_id is the idempotency key | ST12 |
| 7 | Flooding or one noisy client | gateway, scoring | Abusive or compromised client | Per-client rate limit, pod resource limits, HPA, timeouts, circuit breaker toward Cassandra (`store/guarded.py`). Accepted risk: while Cassandra is failing, payments are scored without history signals (`degraded=true`) | ST7 |
| 8 | Personal data leaks through storage or logs | data, logs | Anyone with read access to storage or logs | HMAC-SHA256 pseudonyms for user and card ids, log scrubbing, encrypted volumes | ST11 |
| 9 | Someone denies or hides a change | audit | Insider hiding an action | Append-only audit table, each row hashes the previous row | ST8 |
| 10 | Poisoned labels or training data | feedback path | Attacker feeding bad labels | Only the label service identity writes labels, batch sanity checks, shadow mode, promotion gate, frozen golden set | ST13 |
| 11 | Tampered model file | registry, scoring | Attacker with registry or storage access | SHA-256 recorded at registration and checked at load against a value pinned in the deploy config, outside the model folder (`deploy/scoring.env`, week 2). A live change needs a signed promotion message carrying the hashes (`registry/`, week 3) | ST9 |
| 12 | Attacker forces retrains or slowly shifts the data | drift-job | Attacker manipulating drift signals | Retrain cooldown, weekly retrain cap, golden-set comparison before promotion | ST14 |
| 13 | Vulnerable or malicious dependency | build | Supply-chain attacker | Pinned versions, Trivy and pip-audit in CI, gitleaks | ST10 |
| 14 | Attacker probes the API to learn what gets approved | scoring | Fraudster probing for approval rules | Hide scores from clients, query budget per client, probe detection | E6 |

## Test definitions
| ID | Test | Pass condition |
|----|------|----------------|
| ST1 | 1,000 requests with forged, expired, altered or alg: none tokens | 100% get 401, none reach scoring-svc |
| ST2 | Each role calls every route | Matrix matches the Roles table, forbidden calls get 403 |
| ST3 | Test pod outside the mesh calls scoring-svc directly | Connection refused or rejected |
| ST4 | ingest-svc tries to open a connection to Postgres | Blocked by NetworkPolicy |
| ST5 | Schemathesis fuzzing of the OpenAPI file, 1,000+ cases | No 5xx, no crashes |
| ST6 | Injection strings in every text field | No change in any data store, no error leak |
| ST7 | One client sends 200 req/s against a limit of 50 | Excess gets 429, P95 for others moves under 10% |
| ST8 | Edit one audit row by hand, run the verify tool | Tool names the first broken row |
| ST9 | Swap the model file for another | Scoring refuses the swapped file and keeps serving the old model |
| ST10 | CI scan of repo and images | No secrets, no unwaived HIGH or CRITICAL findings |
| ST11 | Search all logs and stored rows for raw user ids | Zero hits |
| ST12 | Same signed request twice, and one with a 10 min old timestamp | One stored row, old one rejected |
| ST13 | Write to labels with a service account that is not label-sim | Write denied |
| ST14 | Force 5 drift alarms in one day | At most the allowed retrains run, cooldown message logged |
| E6 | Evasion study: probing the API | Clear drop in evasion success at Q = 50 after defenses, false positives up by no more than 0.1 point |
