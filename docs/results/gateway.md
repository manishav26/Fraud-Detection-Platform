# Gateway check (week 1, steps 2-4)
Run on 2026-10-19 after `make up`.

| Request | Result |
|---|---|
| POST /token | 200, RS256 token with scope score:write |
| POST /v1/transactions/score with valid token | 200, `{"txn_id":"t1","score":0.12,"decision":"allow","model_version":"stub-0"}` |
| Same call with no token | 401 `{"message":"Unauthorized"}` |
| Same call with a bad token | 401 `{"message":"Bad token; invalid JSON"}` |

Connectivity: `rpk cluster info`, `psql` (select 1) and `cqlsh` (describe cluster) all connect.

k3d: `make cluster` gives one node, `kubectl --context k3d-fraud get nodes` shows `k3d-fraud-server-0 Ready control-plane`.

Object store: MinIO no longer ships free images or binaries (Docker Hub and Quay refuse, the Homebrew download returns 410),
so `compose.yaml` runs RustFS, which is S3-compatible. Check: created a bucket, wrote and read an object over the S3 API.
