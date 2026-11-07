# Cases and the audit log (week 3, step 5)

Compose stack. Design: `docs/alerts.md`.

## Cases from real payments

`make replay SPEED=0 LIMIT=2000`, all 2,000 answered 200: approve 1,958, review 25, decline 17.

| Check | Result |
|---|---|
| Cases opened | **42 = 25 review + 17 decline**; approved payments opened none; nothing unreadable |
| In Postgres | 25 `review` and 17 `decline`, all `open` |
| Same 2,000 payments replayed again | No new cases: `cases_opened` stayed 42, `already_open` 42 |
| `make verify-audit` | `OK: 42 rows, chain intact`, one audit row per case |

## ST8: an audit row edited by hand

The database owner switched the append-only trigger off, changed the 3rd row's actor to
`attacker`, and switched the trigger back on (`make st8-tamper ROW=3`).

| Step | Result |
|---|---|
| After tampering | `BROKEN at row 47: its contents were changed after it was written` |
| After putting it back | `OK: 42 rows`, latest hash `7bd26501...` as before |
| Tampering with a row that doesn't exist | Stopped with an error, changed nothing |
| `DELETE FROM audit_log` as the owner, trigger on | `audit_log is append-only: DELETE refused` |

## Analyst routes (compose, through Kong)

| Check | Result |
|---|---|
| `GET /cases` without a token | 401 from Kong |
| List and read cases with a token | Newest first, with reasons |
| Resolve case 48 as `not_fraud` with a note | Saved with `resolved_by: dev-client` and the time |
| Resolve it again | 409 `case already resolved` |
| Resolve with a forged `X-Consumer-Username: boss` header | `resolved_by: dev-client`: Kong replaced the header |
| Resolve a case that doesn't exist | 404 |
| `make verify-audit` | `OK: 44 rows` (42 opened + 2 resolved) |

## On Kubernetes, 2 copies of alert-svc

- **Partitions split 3 and 3** between the two copies (`rpk group describe alert-svc`), lag 0.
- **One copy killed during a replay of 3,000 real payments:** all 3,000 answered 200, with 60
  flagged (33 review + 27 decline). Checked one by one against Postgres: **all 60 have a case, none
  twice, each with the right decision, and no approved payment has one.** The other 23 cases came
  from older messages already in the topic.
- **ST8 on the cluster:** `BROKEN at row 3` after tampering, `OK: 83 rows` after restoring.
- Analyst route works on port 8080.

The replay had 247 degraded payments (8%), with Cassandra healthy, on a laptop also running the
cluster. To be looked at with the metrics in step 7.

**ST8 passes:** the tool named the first broken row. The 3rd row has id 47 because ids have gaps
(the tests' rolled-back transactions use ids too); the chain links rows by hash, not by id.
