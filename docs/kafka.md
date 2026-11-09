# Redpanda logins and topic permissions (week 5, step 1)

The network rules (`docs/network.md`) and Linkerd (`docs/mtls.md`) decide **who may connect** to
Redpanda: ingest, scoring, alert-svc, the feature job and model-admin. They can't decide **what
each may do** once connected. Before this step any of them could read or write any topic, so a
compromised ingest could publish to `model.events` without the audit row
(`docs/results/model_admin.md`, "Known limit"), or read every score from `txn.scored`.

Now every workload logs in as its own Redpanda user, and topic permissions (ACLs) allow only
what it needs. Anything not allowed is refused.

## Users and what they may do

| User | May | Used by |
|---|---|---|
| `ingest` | write `txn.raw`, `txn.scored`, `dlq.txn` | ingest-svc |
| `scoring` | read `model.events` | scoring-svc, following promotions (no consumer group) |
| `alert-svc` | read `txn.scored`, as consumer group `alert-svc` | alert-svc |
| `feature-job` | read `txn.raw` (and the test topics `txn.parity`, `txn.bench`), with Spark's own group names (prefix `spark-kafka-source`) | the feature job, Spark or Python |
| `model-admin` | write `model.events`: **the only user that may publish a promotion**, and model-admin-svc writes the audit row first | model-admin-svc |
| `tools` | write `txn.parity`, `txn.bench` | laptop tools through the tunnel (`stream/load_raw.py` for the Spark runs) |
| `admin` | everything: topics, users, permissions | `rpk` inside the Redpanda pod only (`scripts/rpk.sh`) |

Producers that use idempotence (ingest, model-admin) also have `idempotent_write` on the cluster.
Users for Manisha's label and drift jobs are added when those run on the cluster.

## How it works

- **Logins:** SASL with SCRAM-SHA-256: the password is checked without crossing the wire. The
  connection is `SASL_PLAINTEXT` because Linkerd already encrypts it (mTLS); the laptop's tunnel
  runs inside kubectl's own TLS connection.
- **Passwords:** random, in the encrypted secrets file like every other (`docs/secrets.md`).
  Each workload gets a Secret `kafka-<name>` with `KAFKA_USER` and `KAFKA_PASSWORD`.
  `kafka_login.py` turns those into the client's settings (aiokafka, and Spark's Kafka source).
- **Compose stays open:** with no `KAFKA_USER` a client logs in as no one. Dev only, like
  compose's dev passwords; unit tests need nothing.
- **Setting it up** (`scripts/kafka_security.sh`, run by `make k8s-up`, safe to rerun): the
  superuser, then the users (created, or given their current password), then the permissions,
  then logins on (`enable_sasl`, which also turns permission checks on, no restart). The order
  matters: Redpanda refuses a login while logins are off (`ILLEGAL_SASL_STATE`), and everyone
  without one once they're on. On the first switch, services restarted by the deploy retry until
  logins are on; new ingest pods stay not ready meanwhile, so the old ones keep serving.
- **rpk as admin** (`scripts/rpk.sh`): the admin password goes into the pod on stdin and into
  rpk's environment there, never on a command line. The topic setup, ST1's message count and the
  Spark runs use it.
- **The admin API** (port 9644: user management, Prometheus's metrics) has no login: the network
  rules let only Prometheus and the pod itself reach it. Turning on its login is possible
  (`admin_api_require_auth`) but would need Prometheus to log in as well.

## Check

`make k8s-kafka-check` (`security/tests/kafka_acl_check.py`, recorded under ST4): each user tries
what it may and may not do, through rpk in the Redpanda pod. No login and a wrong password must
be refused; ingest may not write `model.events`; scoring, alert-svc and the feature job may not
write; tools may not touch `txn.raw` or `model.events`. Allowed reads must work. It writes one
marked test message to `dlq.txn` only: a junk message on `model.events` would replace the live
promotion, so the allowed writes there and to `txn.raw` are shown by a real promotion and replay.

## Result (10 Nov)

19 of 19 checks as expected, and the platform kept working with every service logged in
(`docs/results/security.md`, "ST4, Kafka permissions").

## Limits

- **Old permissions stay** if a user's list shrinks later: the script adds, it doesn't remove.
  `rpk security acl list` shows them; delete one with `scripts/rpk.sh security acl delete`.
- **The admin API has no login** (above).
- **One broker:** a 3-broker cluster would need the same users on every broker, which Redpanda
  shares between brokers by itself.
