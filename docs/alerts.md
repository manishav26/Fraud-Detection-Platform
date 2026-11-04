# Alerts, cases and the audit log (week 3, step 5)

```
ingest -> txn.scored -> alert-svc: review or decline? -> case + audit row, in one transaction
analyst -> Kong -> alert-svc: list, read, resolve a case -> audit row
make verify-audit -> recomputes the chain -> names the first broken row (ST8)
```

All five parts are done: ingest publishes to `txn.scored`; the tables, audit chain and
`verify_audit`; alert-svc opening cases; analyst routes; and alert-svc on Kubernetes, with 2 copies
sharing the work (`deploy/k8s/base/alert-svc.yaml`).

## Analyst routes (through Kong, with a token)

| Route | Does |
|---|---|
| `GET /cases?status=open&limit=50` | Newest first; status `open`, `fraud` or `not_fraud`; up to 200 |
| `GET /cases/{id}` | One case, 404 if none |
| `POST /cases/{id}/resolve` | Body `{"outcome": "fraud" or "not_fraud", "note": "..."}`; 409 if already resolved |

- **Resolving and its `case_resolved` audit row** are saved together, or neither.
- **A decision is never silently overwritten:** the UPDATE only matches an `open` case, in one
  statement, so two analysts resolving at once can't both win. The second gets 409.
- **Who resolved it** comes from the `X-Consumer-Username` header that Kong sets after checking the
  token, replacing any a client sends. A request without it didn't come through Kong and gets 403.
  For now every token belongs to one dev client, so the name isn't a real person yet: step 6 gives
  each analyst their own identity and role, and NetworkPolicies (week 4) stop anyone reaching
  alert-svc without going through Kong.
- `/stats` isn't routed through Kong.

## alert-svc (`services/alert-svc/`)

- **Consumer group `alert-svc`:** copies of the service share the 6 partitions, so each message is
  handled once. The opposite of scoring's promotions, where every pod reads everything.
- **Starts from the beginning of the topic** the first time, so earlier flagged payments get cases.
- **At-least-once:** a message's position is saved only after its case is saved. After a crash,
  messages are read again, and `txn_id` being unique makes that harmless.
- **One transaction per case:** the case and its `case_opened` audit row, both or neither.
- **Unreadable message:** skipped, logged and counted. Retrying it would block every case behind it.
- **Postgres or Redpanda down:** it starts over from the last saved position every 5 s.
- **Its own login** (`alerts`), which can't change the audit log.
- `GET /stats` (`make alert-stats`) shows counts until the metrics in step 7.

## Tables (`deploy/postgres/platform.sql`, `make db-schema`)

- **`cases`:** one per flagged payment. `txn_id` is unique, so a message delivered twice can't open
  two cases. Status: `open`, `fraud` or `not_fraud`, with who resolved it and when.
- **`audit_log`:** who did what, append-only.
- **A separate step:** Postgres runs its startup scripts only on an empty disk, and ours already has
  MLflow's data. `make k8s-up` runs it on the cluster too. Safe to run again.

## Audit log protection

| Layer | Stops |
|---|---|
| alert-svc's login (`alerts`) may only add and read audit rows | A compromised alert-svc changing history |
| A trigger refuses UPDATE, DELETE and TRUNCATE, even for the database owner, per statement (a per-row trigger never fires when no row matches) | Mistakes and casual edits by anyone |
| Hash chain, checked by `make verify-audit` | Edits by someone who turns the trigger off: names the first broken row |

**The chain:** each row stores SHA-256 of the previous row's hash plus its own time, actor, action,
target and details, written in one fixed form (sorted keys, UTC time to the microsecond). Change,
remove or reorder a row, and from there on the hashes stop matching.

**Two appends at once:** each takes a lock until its transaction ends, so two rows can't both claim
the same previous row. It's an advisory lock, because locking the table needs UPDATE rights, which
the append-only login must not have.

**Same transaction as the change it records:** a case and its audit row are saved together or not
at all.

**Ids have gaps.** Postgres never reuses an id, even one taken by a transaction that was undone, as
the tests' are. The chain links rows by hash, not by id, so gaps don't matter to `verify-audit`.

## Known limits

- **Rows removed from the end aren't detected,** and someone with owner rights could rebuild the
  whole chain. Both are caught by anchoring: saving the latest hash somewhere they can't reach.
  Planned later.
- The `alerts` password is a dev value in `platform.sql`. Secrets are week 4 work.
