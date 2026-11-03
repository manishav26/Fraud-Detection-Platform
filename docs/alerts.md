# Alerts, cases and the audit log (week 3, step 5)

```
ingest -> txn.scored -> alert-svc: review or decline? -> case + audit row, in one transaction
analyst -> Kong -> alert-svc: list, read, resolve a case -> audit row
make verify-audit -> recomputes the chain -> names the first broken row (ST8)
```

Done so far: ingest publishes to `txn.scored` (commit 1), and the tables, audit chain and
`verify_audit` (commit 2). alert-svc comes next.

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

## Known limits

- **Rows removed from the end aren't detected,** and someone with owner rights could rebuild the
  whole chain. Both are caught by anchoring: saving the latest hash somewhere they can't reach.
  Planned later.
- The `alerts` password is a dev value in `platform.sql`. Secrets are week 4 work.
