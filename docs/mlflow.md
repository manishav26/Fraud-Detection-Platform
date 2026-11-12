# MLflow model registry

The catalogue of model versions, and the label saying which one is live. Includes the server, the
register and promote tools, and live reload in scoring, on compose and on Kubernetes (`docs/k8s.md`).

Image: `services/mlflow/`. Tools: `registry/`. Check: `make mlflow-check`.
Web page: http://localhost:5050

## How the pieces fit

```
client -> MLflow server :5000 -> Postgres, database `mlflow`   (versions, aliases, runs)
                              -> RustFS, bucket `mlflow`       (the model files)
```

## Why these choices

- **Version 3.17.2, pinned.** Checked in its source: stages are deprecated since 2.9, and
  `set_registered_model_alias` is the current way to mark a version.
- **Alias `production` instead of a `Production` stage**, for that reason. Moving the alias to
  another version is how a model goes live or rolls back.
- **MLflow serves the files itself** (`--artifacts-destination`). Clients talk only to MLflow, so
  scoring and Manisha's training never need the object store's password or address. This needs a
  setting on each **client**: since 3.17 the server offers direct links to the object store, and
  the client uses them by default (`objectstore:9000`, unreachable from a laptop, so downloads
  retried for many minutes). `registry/__init__.py` turns that off.
- **Its own Postgres database and user**, so MLflow can't touch platform tables.
- **Allowed hosts are set explicitly.** MLflow rejects requests for host names it doesn't know,
  which protects against DNS rebinding. `mlflow` works in compose and Kubernetes.
- **Named volumes for Postgres and RustFS.** The registry survives `make down`. To really wipe
  it: `docker compose down -v`.
- **No root, 1 worker, 1 GB limit, laptop-only port.**
- **Background jobs off** (`MLFLOW_SERVER_ENABLE_JOB_EXECUTION=false`). By default MLflow starts
  job workers for GenAI features, about 175 MB each. They pushed it past the 1 GB limit, so the
  kernel killed it about every 25 s (exit 137). The registry doesn't use them.
- **Database driver named in the address** (`postgresql+psycopg2://`). Plain `postgresql://` makes
  SQLAlchemy look for the `psycopg` package, which crashed the first start.

## Register and promote

```
make register   load-check the folder -> upload -> new version       (not live)
make promote    download that version -> load-check -> promotion gate vs the live model
                -> a person checks the hashes -> sign {model, version, hashes, sequence,
                action, label, reason, gate} -> POST /v1/model/promote through Kong (admin)
                -> model-admin-svc: check signature and rules -> audit row -> model.events
                -> move the `production` alias
make rollback   the same for the version live before the current one, without the gate
```

Until week 4, `make promote` published to `model.events` itself. Now every change goes through
model-admin-svc, so each one has an audit row naming the admin who sent it.

## Admin promote and rollback (week 4, step 6)

**The signing key stays on the admin's laptop.** model-admin-svc has only the public key: it can
accept a genuine promotion but never create one. So a stolen admin token alone can't make any
model live, and the ST9 guarantee ("the key never enters a container") still holds.

| Route (admin only, through Kong) | What it does |
|---|---|
| `GET /v1/model` | What is live (version, label, who, when, reason), and `previous`: what a rollback would go back to |
| `GET /v1/model/history` | Every promotion, rollback and failed publish, newest first |
| `POST /v1/model/promote` | `{"token": <signed promotion>}` |
| `POST /v1/model/rollback` | `{"token": <signed promotion of the previous version>}` |

What model-admin-svc checks before publishing (`services/model-admin-svc/app/rules.py`):

| Check | Refused with |
|---|---|
| Caller is `admin` (Kong's `acl`, then again from Kong's headers) | 403 |
| Signature, issuer, audience (the same check scoring makes) | 400 |
| The message is for this route (`action` promote or rollback) | 409 |
| Its sequence is newer than every recorded attempt, failed ones too | 409: a token is used once |
| Promote: the gate's result is in the message; if the gate said keep, a reason is given | 409 |
| Rollback: a reason is given, and the version is exactly the one live before the current one | 409 |

- **The audit row is saved before publishing.** No model change ever goes out unrecorded. If the
  publish then fails, a second row (`model_promotion_failed`) says so, and the token can't be
  reused: signing again gives a new sequence.
- **Check and record happen in one transaction, holding the audit chain's lock**, so two
  promotions sent at once can't both pass the sequence check.
- **The gate runs on the laptop, before signing**, because it needs the training data and both
  models (`ml/eval/promotion_gate.py`: +0.01 PR-AUC, score + reasons p95 at most 1.2 times the live model's on the same machine, at most 2%
  flagged). Its verdict and numbers are signed into the message, so the audit row shows them and
  an override can't be hidden. `OVERRIDE="why"` promotes past a "keep" and records why.
- **A rollback skips the gate:** it is the way back when a model goes wrong, and the version it
  returns to was live a moment ago. It must give a reason. Rolling back twice goes forward again.
- **Its own database login** (`model_admin`): may read and add audit rows, nothing else, not even
  cases.
- **One copy** on the cluster: admins send a few requests a day.

| Choice | Why |
|---|---|
| Registering and promoting are separate | Training can add versions but can't make one live: that takes the private key |
| A new key pair in `promotion-keys/`, not `keys/` | `keys/` is mounted into the token issuer. A leaked login key or a compromised issuer must not be able to promote. No container mounts this folder, and it is in `.gitignore` and `.dockerignore` |
| Algorithm fixed by the checker | A forged message can't choose `alg: none` or use the public key as a shared secret |
| Audience and issuer are checked | A signed token meant for something else isn't accepted as a promotion |
| No expiry | The latest message stays in the compacted topic, and a restarting pod must still accept it |
| `sequence` = time in milliseconds | Always rises and needs no lookup. Scoring refuses a lower one, so re-sending an old real message can't bring back an old model. Rollback is a new promotion of the old version |
| A person checks the hashes | The signature promises only what was signed. If the registry was tampered with first, this is the last point to catch it |
| Publish first, alias after | The signed message decides what is live; the alias only shows it to people. If moving the alias fails, promoting again repairs it |
| 10 s limit on publishing | aiokafka alone retries forever while the broker is down (`docs/ingest.md`) |
| `mlflow-skinny` in the venv | Same version as the server, and it fits all our pins (checked with a dry-run install) |

## Live reload in scoring (`registry/live_model.py`)

Each pod reads `model.events` itself, in the background:

```
message -> signature OK? -> for this model? -> newer sequence?
        -> download through MLflow -> files match the signed hashes? -> load fully -> swap
```

| Situation | What happens |
|---|---|
| Forged or junk message | Refused and logged; current model keeps serving |
| Files changed after signing | Refused, not retried. An older genuine promotion is used if it is newer than what's running |
| Old genuine message sent again | Refused: its sequence is lower |
| Same message read again (reconnect) | Ignored |
| MLflow down | Current model keeps serving; retried every 30 s |
| New model fails to load | Current model keeps serving; retried every 30 s |
| Redpanda down, or topic missing | Current model keeps serving; reconnects every 5 s |
| Pod starts | Pinned model loads, then it reads the topic, for up to 30 s, before opening its port |

- **Swap without errors:** the new model loads completely in a background thread. Then model and
  version name are replaced together in one step. Each request takes one copy of the pair, so it
  uses one model from start to finish.
- **`model_version` names the exact files** (week 4): the model's name plus the first 8 characters
  of its `model.txt` SHA-256, for example `fraud-lgbm-v1-535eb73c`. It is the same whether the
  model was pinned at startup or promoted, so one model never has two labels and two models never
  share one. (Before, a pinned model was labelled by its folder name and a promoted one by its
  registry version, so registry version 1 and the folder `fraud-lgbm-v1` could be different
  models with the same label.) The hash in the label has always just been checked, against the
  pins or the signed promotion. The registry version is in the "now serving" log line.
- **No consumer group:** in a group, only one pod would receive each message.
- **Redpanda keeps its data now** (a named volume), or `make down` would forget the live model.

## Known limits

- **MLflow has no login here.** Anyone who can reach the port can register a model or move the
  alias. That is why the alias alone never makes a model live: scoring will trust only a signed
  promotion message, which carries the file hashes. On Kubernetes, NetworkPolicies will limit who
  can reach it (week 4).
- **A stolen private key defeats this.** It lives only on the promoter's machine for now.
- **Each laptop has its own key pair and registry**, because they are generated locally. A shared
  deployment would have one public key in its config.
- **Someone with write access to the topic can't forge a promotion, but could delete messages,**
  for example with an empty or junk message under the same key, which compaction then keeps. A
  restarting pod would fall back to the pinned model. Since week 5 only model-admin's Redpanda
  user may write the topic (`docs/kafka.md`).
- **model-admin-svc is the only way onto `model.events`** on the cluster since week 5: its
  Redpanda user is the only one allowed to write it (`docs/kafka.md`). In compose Redpanda is
  open, so there anything could still publish a signed promotion without an audit row.
- **Pods can differ for a moment** during a swap, each answering with its own `model_version`.
- Postgres runs its setup script only when its data folder is empty. If the script changes, the
  volume must be recreated.
- Passwords in `compose.yaml` are for local development only. Secrets are week 4 work.
