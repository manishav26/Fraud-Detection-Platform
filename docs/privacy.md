# Pseudonyms for user ids (week 5, step 2; threat 8, ST11)

The user id is the card's proxy id, e.g. `9500|204.0|98.0` (card1 | addr1 | first-seen day).
Until this step it was stored as sent: in every Redpanda message, as the key of the Cassandra
tables, and in `cases`. Anyone who could read storage could see whose payments they were.

## Design: pseudonymize once, at the edge

**ingest-svc replaces the user id with its pseudonym before anything is stored**
(`pseudonym.py`). Everything after it only sees the pseudonym: Redpanda, scoring, Cassandra, the
feature job, cases, the SIEM.

```
9500|204.0|98.0   ->  p1_3f2a…   (HMAC-SHA256 with the key, first 32 hex characters)
9500|NA|98.0      ->  NA|p1_c41b…  (an unknown card stays unknown to is_known)
```

| Detail | Choice | Why |
|---|---|---|
| Function | HMAC-SHA256 with a secret key | without the key nobody can test a guessed id, which a plain hash allows. 128 bits: no collisions at our scale |
| Format | `p1_<32 hex>`; `NA\|p1_<32 hex>` for unknown cards | `p1` is the key version. Scoring's `is_known()` works unchanged, so unknown cards still get no history, as in training |
| Features | **identical** | one id always gives the same pseudonym, and history only groups payments by user (`tests/test_pseudonym.py` checks training's code on both) |
| Key | `PSEUDONYM_KEY` in the encrypted secrets file; on the cluster only ingest gets it (Secret `pseudonym-key`) | compose uses a dev key, like its dev passwords; the secrets check refuses a dev value on the cluster |
| Laptop tools | the backfill, read timing, the parity tool and `load_raw` pseudonymize the same way, with the key from `scripts/k8s_secrets.sh run` | their history rows must match what ingest writes |
| Bad requests | a rejected body that is JSON with a text `user_id` is kept in `dlq.txn` with that field pseudonymized | the dead-letter queue is the one place raw input is kept on purpose |
| Applied twice | a pseudonym stays as it is | retries and tools rerun on converted data are safe |
| Looking a customer up | `make k8s-pseudonym ID='9500\|204.0\|98.0'` (with your age key) prints the pseudonym to search cases for | a pseudonym can't be turned back into the id |

**Where user ids are now:**

| Where | Holds |
|---|---|
| Redpanda `txn.raw`, `txn.scored`, `dlq.txn` | pseudonym |
| Cassandra `txn_by_user`, `features_by_txn`, `user_features`; the feature job's checkpoint | pseudonym |
| Postgres `cases.user_id` (analysts see it) | pseudonym |
| audit log, SIEM events, service logs | no user id at all (they carry `txn_id`), as before |
| the client's own request, ingest's memory for the moment it runs | the raw id |

## The data stored before (`make k8s-reset-data`, one-way)

Earlier runs left raw ids on the laptop's cluster, and mixing them with pseudonyms would also split
every card's history in two. `scripts/k8s_reset_data.sh` (asks for `yes`):
1. stops ingest
2. empties the three Cassandra tables
3. recreates `txn.raw`, `txn.scored` and `dlq.txn`
4. starts the feature job from a fresh checkpoint
5. rewrites `cases.user_id` in place with Postgres's own HMAC (pgcrypto, checked against
   `pseudonym.py` on one case first, removed afterwards)
6. reloads the history with the backfill, now pseudonymized, and starts ingest again

Everything except the cases is rebuilt from the parquet files. Cases, the audit log and the SIEM
chain stay: the audit rows never held user ids. A new cluster (the AWS VM) starts empty and needs
none of this.

## Check: ST11 (`make k8s-st11`)

Replays 500 payments, then searches every store and log for those payments' raw ids and for
anything raw-shaped (`card1|addr1|day`): Cassandra's three tables, `cases` and the audit log,
every message in the three payment topics, the SIEM's events, and the logs of every container
since the replay. Every stored id must also have the pseudonym's shape. Pass: 0 hits.

## Result (10 Nov)

ST11 passed on the cluster after the reset: no raw user id anywhere, every stored id a pseudonym.
A replay afterwards gave 840 cold starts against 837 before, so history still lines up
(`docs/results/security.md`, "ST11").

## Limits

- **The payment's attributes stay as sent.** `card1`, `addr1` and the other model features are in
  `txn.raw` and `txn.scored`, because the model needs them. They are the dataset's own masked
  codes, not card numbers, but someone who can read those topics could rebuild an id from them.
  Only ingest may write them and only alert-svc and the feature job may read them
  (`docs/kafka.md`). Hashing them would mean retraining the model.
- **No key rotation:** a new key gives every card a new id, cutting it off from its history.
  `p1` leaves room for a `p2` with a run that re-keys every stored id.
- **A rejected body that isn't JSON, or has a non-text `user_id`, is kept as sent** in `dlq.txn`
  (up to 10,000 characters).
- **Volumes aren't encrypted** on the laptop's cluster (threat 8 also lists encrypted volumes);
  on a cloud VM the provider's disk encryption would cover it.
- **Compose uses the dev key**, so its pseudonyms are guessable by anyone who reads the code.
