# Gateway check (week 1, steps 2-4)
Run on 2026-10-19 after `make up`.

| Request | Result |
|---|---|
| POST /token | 200, RS256 token with scope score:write |
| POST /v1/transactions/score with valid token | 200, `{"txn_id":"t1","score":0.12,"decision":"allow","model_version":"stub-0"}` |
| Same call with no token | 401 `{"message":"Unauthorized"}` |
| Same call with a bad token | 401 `{"message":"Bad token; invalid JSON"}` |

Connectivity: `rpk cluster info`, `psql` (select 1) and `cqlsh` (describe cluster) all connect.

Known gap: MinIO is under the `storage` compose profile because its image could not be pulled from
Docker Hub or Quay. It is not needed until MLflow artifacts in week 3.
