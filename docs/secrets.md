# Cluster passwords (week 4, step 5 part B)

Threat 13 (with the supply-chain scans in `docs/supply-chain.md`): a password committed to git
can be read by anyone with the repo, and since it is the same everywhere, it opens every copy of
the system. Until this step, the cluster's eight passwords were written in `scripts/k8s_up.sh`.

Control: each password is a random 192-bit value, stored in git **only encrypted**
(`deploy/k8s/secrets.enc.env`, SOPS with age). The key that decrypts it lives only on the
laptop. `make k8s-up` decrypts them into memory, builds the cluster's Secrets from them, and
changes every database login to match.

Compose keeps its dev passwords (`*_dev_only`): it is a local throwaway, and having them visible
makes it easy to poke at.

## The pieces

| File | In git | What it is |
|---|---|---|
| `.sops.yaml` | yes | which file is encrypted for which age **public** key (it can only encrypt) |
| `deploy/k8s/secrets.enc.env` | yes | the passwords and tokens; names in the clear, values encrypted (AES-256-GCM) |
| `~/.config/sops/age/keys.txt` | **never** | the age private key; the only thing that can decrypt the file |
| `scripts/gen_secrets.sh` | yes | makes the passwords and encrypts them (`make k8s-secrets`) |
| `scripts/k8s_secrets.sh` | yes | decrypts into memory: for k8s-up, laptop tools (`run`), and `show` |

| Password | Login | Used by |
|---|---|---|
| `POSTGRES_PASSWORD` | Postgres `fraud` (owner) | schema scripts, `make k8s-verify-audit` |
| `MLFLOW_DB_PASSWORD` | Postgres `mlflow` | MLflow |
| `ALERTS_DB_PASSWORD` | Postgres `alerts` | alert-svc |
| `MODEL_ADMIN_DB_PASSWORD` | Postgres `model_admin` | model-admin-svc |
| `CASSANDRA_ADMIN_PASSWORD` | Cassandra `fraud_admin` | schema script, `make k8s-backfill`, `make k8s-read-timing` |
| `CASSANDRA_SCORING_PASSWORD` | Cassandra `scoring` | scoring-svc |
| `CASSANDRA_FEATURES_PASSWORD` | Cassandra `features` (week 4, step 1) | the feature job (`docs/spark.md`) |
| `OBJECTSTORE_SECRET_KEY` | RustFS | RustFS itself, MLflow |
| `GRAFANA_ADMIN_PASSWORD` | Grafana `admin` | you, in the browser (`make k8s-password`) |
| `SIEM_TOKEN` | the SIEM's collector token (week 4, step 7) | alert-svc sends with it, siem-mock checks it (`docs/siem.md`) |
| `REDPANDA_ADMIN_PASSWORD` | Redpanda superuser `admin` (week 5) | `rpk` in the Redpanda pod only (`scripts/rpk.sh`) |
| `REDPANDA_INGEST_PASSWORD`, `_SCORING_`, `_ALERTS_`, `_FEATURES_`, `_MODEL_ADMIN_` | one Redpanda user per workload | that workload, through its `kafka-<name>` Secret (`docs/kafka.md`) |
| `REDPANDA_TOOLS_PASSWORD` | Redpanda user `tools` | laptop tools through the tunnel, via `scripts/k8s_secrets.sh run` |

The token issuer's signing key and the clients' secrets are separate: `make keys` generates them
into `keys/`, which isn't in git (`docs/access.md`). The promotion key stays on the laptop
(`docs/mlflow.md`).

## How it works

```
make k8s-secrets   openssl rand -> memory -> sops --encrypt -> deploy/k8s/secrets.enc.env (commit)
make k8s-up        sops --decrypt -> memory -> kubectl create secret ... (on stdin) -> cluster
                   -> ALTER ROLE ... for each database login -> restart our services
```

- **Plain values never touch a file.** The generator writes them only to a private temporary
  folder (`umask 077`, deleted on exit) for sops to read. k8s-up keeps them in shell variables
  and hands each Secret to `kubectl` on stdin. Statements that contain a password go to psql and
  cqlsh on stdin too, so they don't show up in `ps`.
- **The logins follow the file.** A full `make k8s-up` sets every Postgres login with
  `ALTER ROLE` (psql in the Postgres pod uses the local socket, which needs no password, so it
  works whatever the password was before). For Cassandra, `create_schema.sh` logs in with the new
  admin password, or else the previous one (kept in the Secret `cassandra-admin-credentials`; the
  dev one the first time) and changes it, then sets `scoring`'s. Grafana keeps no state between
  restarts, so it takes its password on each start. RustFS reads its key when it starts, so k8s-up
  restarts it when the key changed.
- **Order:** on an existing cluster, the logins are changed before our services restart, so no
  pod restarts with a password the database doesn't know yet.
- **`make k8s-apply` refuses after a rotation.** It doesn't change logins, so if the file's
  passwords differ from those the logins were last set to, it stops and asks for a full
  `make k8s-up`. It compares a fingerprint (ConfigMap `secrets-fingerprint`: the first 16 hex
  characters of a SHA-256 of the decrypted file; safe to store, since the passwords are random).
- **Laptop tools** that log into a database through a tunnel get their password from
  `scripts/k8s_secrets.sh run`, which exports it for that one command:
  `make k8s-backfill`, `k8s-read-timing`, `k8s-verify-audit`, `k8s-st4-db`.

## Using it

First time (once per laptop):

```bash
brew install sops age
mkdir -p ~/.config/sops/age && age-keygen -o ~/.config/sops/age/keys.txt
```

Put the printed public key in `.sops.yaml`. Then:

| Task | Command |
|---|---|
| Make the passwords (or add a new one to the list) | `make k8s-secrets`, then commit `deploy/k8s/secrets.enc.env` |
| Put them into the cluster | `make k8s-up` |
| See one, e.g. for Grafana | `make k8s-password` (or `NAME=POSTGRES_PASSWORD`) |
| Rotate all of them | `make k8s-secrets ROTATE=1`, `make k8s-up`, commit the file |
| Check the cluster | `make k8s-secrets-check` and `make k8s-st4-db` |
| Let someone else deploy | they make an age key and send its public half; add it to `.sops.yaml` and run `sops updatekeys deploy/k8s/secrets.enc.env` |

**Back up `keys.txt`** (a password manager is fine). Without it the file can't be decrypted. The
fix is `make k8s-secrets ROTATE=1` and `make k8s-up`, but rotating Cassandra's admin then needs
its current password, which is still in the cluster's `cassandra-admin-credentials` Secret.

## Tests

- `tests/test_secrets.py`, in CI: no dev password in anything the cluster reads (`k8s_up.sh`,
  `deploy/k8s/`, the `k8s-*` Make targets); k8s-up uses all of them and sets every Postgres login;
  the encrypted file holds exactly those, all encrypted; a decrypted copy would be ignored by
  git. With sops installed, it also makes, keeps and rotates passwords with a throwaway key.
- `make k8s-secrets-check`, on the running cluster: no Secret holds a dev password, and each
  Postgres login takes its new password and refuses its old dev one (over the pod's own address,
  where passwords count; see Limits).
- `make k8s-st4-db`: the same for Cassandra's two logins, plus what each may do.
- gitleaks (`make scan`) finds nothing in the encrypted file.

## Limits

- **Anyone who can read Secrets in the namespace can read the passwords.** Kubernetes Secrets
  are only base64-encoded, and k3s doesn't encrypt them at rest unless started with
  `--secrets-encryption`. Here only the laptop's admin can read them; a real cluster would add
  encryption at rest or an external secret store (Vault, a cloud KMS).
- **Postgres trusts its own pod.** The image's defaults let the local socket and `127.0.0.1` in
  without a password, which is how k8s-up sets the logins. Other pods arrive on the pod's own
  address, even through the Linkerd proxy (checked: `pg_stat_activity` shows `10.42.x.x`, not
  `127.0.0.1`), so for them the password counts.
- **cqlsh takes its login password as a command-line argument** (`-u`/`-p`), so the admin
  password is briefly visible in `ps` on the laptop and in the Cassandra pod while k8s-up runs.
  Statements that set passwords go on stdin.
- **A fresh cluster runs a few seconds on the dev passwords:** Postgres's first-start script and
  `platform.sql` create the logins with the dev passwords (shared with compose), and k8s-up
  changes them straight after. Services that start before that fail to log in and retry, as
  they already do while the databases start.
- **The RustFS access key** (`minio_dev`) is a user name, not a secret, and stays as it is.
- **Redpanda's logins** (week 5) are in `docs/kafka.md`: one user per workload, limited to its topics.
- **Rotation briefly breaks open connections:** a pod connected before the change keeps working
  until it reconnects, and the restart that follows replaces it.
