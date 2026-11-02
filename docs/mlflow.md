# MLflow model registry

The catalogue of model versions, and the label saying which one is live. Parts 1 and 2 of 4 are
done: the server, and the register and promote tools. Scoring doesn't read the messages yet
(part 3), so a promotion doesn't change the live model yet.

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
make promote    download that version -> load-check -> a person checks the hashes
                -> sign {model, version, hashes, sequence} -> publish to model.events
                -> move the `production` alias
```

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

## Known limits

- **MLflow has no login here.** Anyone who can reach the port can register a model or move the
  alias. That is why the alias alone never makes a model live: scoring will trust only a signed
  promotion message, which carries the file hashes. On Kubernetes, NetworkPolicies will limit who
  can reach it (week 4).
- **A stolen private key defeats this.** It lives only on the promoter's machine for now.
- **Each laptop has its own key pair and registry**, because they are generated locally. A shared
  deployment would have one public key in its config.
- **Someone with write access to the topic can't forge a promotion, but could delete messages.**
  A restarting pod would then fall back to the pinned model.
- Postgres runs its setup script only when its data folder is empty. If the script changes, the
  volume must be recreated.
- Passwords in `compose.yaml` are for local development only. Secrets are week 4 work.
