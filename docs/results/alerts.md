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

**ST8 passes:** the tool named the first broken row. The 3rd row has id 47 because ids have gaps
(the tests' rolled-back transactions use ids too); the chain links rows by hash, not by id.
