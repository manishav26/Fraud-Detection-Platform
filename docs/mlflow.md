# MLflow model registry

The catalogue of model versions, and the label saying which one is live. Part 1 of 4: this is the
server only. Signed promotion and live reload come next.

Image: `services/mlflow/`. Check: `make mlflow-check`. Web page: http://localhost:5050

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
  scoring and Manisha's training never need the object store's password.
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

## Known limits

- **MLflow has no login here.** Anyone who can reach the port can register a model or move the
  alias. That is why the alias alone never makes a model live: scoring will trust only a signed
  promotion message, which checks the file hashes (next commit). On Kubernetes, NetworkPolicies
  will limit who can reach it (week 4).
- Postgres runs its setup script only when its data folder is empty. If the script changes, the
  volume must be recreated.
- Passwords in `compose.yaml` are for local development only. Secrets are week 4 work.
