# Kubernetes (k3d)

Manifests: `deploy/k8s/base/` (Kustomize). Deploy: `make cluster` then `make k8s-up`.
Gateway: `http://localhost:8080` (compose stays on 8000).

## Which deploy command

| What changed | Command | Time |
|---|---|---|
| First time, or a new service | `make k8s-up` | 5–15 min |
| One of our services' code | `make k8s-update SVC=model-admin-svc` | about 1 min |
| Only YAML, config maps or Kong's config | `make k8s-apply RESTART="kong"` | under 1 min |
| Docs, tests, laptop tools | nothing | |

`make k8s-up` restarts everything at once, which keeps the laptop's CPU busy for minutes: the
Kubernetes API can then time out (`TLS handshake timeout`). Wait until `kubectl -n fraud get pods`
answers, then run it again; every step is safe to repeat. Docker Desktop's own Kubernetes should be
off while the k3d cluster runs: both need memory from the same 8 GB.

## What runs where

```
laptop :8080 -> NodePort 30080 -> kong (1) -> ingest-svc (2) -> redpanda (1)
                                           -> token-issuer (1)   -> scoring-svc (2) -> cassandra (1)
```

All in namespace `fraud`, on a k3d cluster of 3 nodes (1 server + 2 agents, all containers on
your laptop). Only Kong is reachable from outside; everything else is a ClusterIP Service.

| Piece | Kind | Why |
|---|---|---|
| ingest-svc, scoring-svc | Deployment, 2 replicas | stateless: copies share the load, and one can die without an outage |
| kong, token-issuer | Deployment, 1 replica | enough for dev |
| redpanda, cassandra, postgres, objectstore | StatefulSet, 1 replica, with a disk | they hold data: topics and the live model, user history, the registry's records and files. 3-copy versions come later |

## Reaching the cluster from the laptop

MLflow has no login, so it is only a ClusterIP Service. `make k8s-register`, `make k8s-promote`,
`make k8s-backfill` and `make k8s-mlflow` go through `kubectl port-forward` tunnels:

- **Nothing new is open** outside the cluster, and the tunnels close when the command ends.
- **The promotion private key never enters the cluster:** signing happens on the laptop.
- **Stops if a port it needs is taken.** Compose uses the same ports, and a tunnel that failed to
  open could otherwise send the command to compose instead.
- The cluster's registry and history are separate from compose's, so load them separately.

## Storage

Each data service has a PersistentVolumeClaim on k3d's built-in `local-path` storage, so its data
survives a pod restart. Two things to know:

- **A disk belongs to one node.** The pod has to return to that node, so if the node is lost, so
  is the service and its data. Real fault tolerance needs 3 copies across nodes.
- **`make k8s-down` deletes the disks too,** because it deletes the namespace. That includes the
  model registry and live model. To pause and keep everything: `k3d cluster stop fraud`.

## Where config comes from

| Object | Made from | Used by |
|---|---|---|
| Secret `jwt-private-key` | `keys/private.pem` (not in git) | token-issuer |
| ConfigMap `kong-config` | `deploy/kong/kong.yml` (made by `gen_keys.sh`) | kong |
| ConfigMap `scoring-config` | `deploy/scoring.env`: model path, **model pins**, Cassandra host | scoring-svc |
| Secrets `*-credentials`, `grafana-admin` | `deploy/k8s/secrets.enc.env`, decrypted in memory (`docs/secrets.md`) | databases, our services, MLflow, Grafana |

`make k8s-up` creates these with `kubectl create ... --dry-run=client -o yaml | kubectl apply`
(so it is safe to run again) because Kustomize can only read files inside `deploy/k8s/`.

## How images get in

There is no image registry. `make k8s-up` builds our images with compose (names
`fraud/<service>:dev`, the same in compose and in the manifests), then `k3d image import` copies
them into every node. Public images (Kong, Cassandra, Redpanda, ...) are copied only if a node
doesn't have them yet (`crictl inspecti` in each node): they never change under their tag, and
copying them every time, a few GB into 3 nodes, was most of a deploy's time. Our pods use `imagePullPolicy: Never`, so they can only
run the image we built, never one pulled from the internet with the same name.

Two problems from the first deploy, both fixed:

- `k3d image import fraud/ingest-svc:dev` failed with `content digest ... not found`: Docker
  Desktop keeps images as multi-platform lists, and only this Mac's platform is really there.
  The pods then stopped at `ErrImageNeverPull`. Fix: `docker save --platform linux/<arch>` to a
  tar file, then import the tar.
- Kong was `OOMKilled` (exit 137) again and again: nginx starts one worker per CPU (12 on this
  laptop), which went past the 512 Mi limit. Fix: `KONG_NGINX_WORKER_PROCESSES=2`. The limit did
  its job: it stopped one pod, not the node.

## The model

`make cluster` mounts your `models/` folder into every node at `/models`; scoring pods mount it
read-only. At startup each pod checks the files against the pins in `scoring-config`, exactly as
in compose. A pod that refuses the model never opens its port, so the readiness probe keeps it out
of the Service and Kubernetes keeps restarting it.

## Hardening already in place

- Requests and limits on every container (one noisy pod can't starve the node; threat 7).
- Our 3 services: no privilege escalation, all Linux capabilities dropped.
- Kong admin API off.
- Probes, an HPA, disruption budgets and zero-downtime rollouts: see "Scale and resilience" below.
- Since then: NetworkPolicies (ST4, `docs/network.md`), mTLS (ST3, `docs/mtls.md`), non-root and
  read-only filesystems (`docs/supply-chain.md`), encrypted passwords (`docs/secrets.md`).

## Scale and resilience (week 4, step 2)

Manifests: probes and rollout settings in each Deployment, the HPA and disruption budgets in
`deploy/k8s/base/resilience.yaml`. `tests/test_k8s_manifests.py` reads the YAML, so a later edit
can't drop any of it. Results: `docs/results/scaling.md`.

| What | Setting | Why |
|---|---|---|
| Startup probe | `GET /healthz`, up to 3 min for scoring, ingest, alert-svc | They wait for Cassandra, Redpanda or Postgres and load the model; the other probes don't count until then |
| Readiness probe | `GET /readyz` every 5 s | A pod gets traffic only when it can answer: scoring has its model and Cassandra connection, ingest its Redpanda producer. Before, it was "the port is open" |
| Liveness probe | `GET /healthz` every 10 s, 6 failures in a row | Restart only after a minute of silence. One slow burst (1-2 s, `docs/results/baseline.md`) must never restart a pod. `/healthz` is async, so it answers even while every worker thread is busy scoring |
| Cassandra down | scoring stays ready | It still answers, degraded (`docs/scoring.md`); taking it out of rotation would turn those answers into errors |
| Rollouts | `maxUnavailable: 0, maxSurge: 1` on every Deployment | A new pod must be ready before an old one goes, so a rollout never lowers capacity |
| Pause before stopping | `preStop: sleep 5 s` on our services and Kong | Kong and the Service keep sending to a stopping pod for a moment; it answers them before it exits, instead of a 502 |
| HPA on scoring | 2-8 copies at 60% of the **scoring container's** CPU request (`ContainerResource`); up after 15 s (2 copies per 30 s), down after 2 min (1 per minute) | Scales with load without flapping on short bursts. `replicas` is left out of the Deployment, or every apply would reset it to 2. The scoring container alone: a pod-level metric adds up every container, and the Linkerd proxy once had no CPU request, which left the HPA unable to compute anything from 8 to 10 Nov (`docs/mtls.md`) |
| Scoring CPU request | 500m (was 250m), no CPU limit | The 60% is a share of the request; at 250m it would scale at the baseline's normal 30/s. A CPU limit throttles the process when reached, which shows up as latency spikes, so memory is limited and CPU only requested |
| Kong to ingest | a headless Service, `ingest-pods`; ingest pauses 15 s before stopping | Its DNS name returns each ready pod, so Kong balances across pods and stops choosing a leaving one within 5 s (the DNS cache). Through a normal Service address, Kong's reused connections stayed tied to a pod after it left and failed when it shut down: 16 errors when a drain evicted an ingest pod (`docs/results/scaling.md`) |
| Ingest to scoring | one retry when a connection breaks (not after a timeout) | The same pinning between ingest and scoring; a fresh connection reaches a live pod. Safe because scoring a payment twice gives the same answer and history row. Counted in `ingest_scoring_retries_total` |
| Keep-alive | our services keep idle connections 75 s (`uvicorn --timeout-keep-alive 75`), Kong 60 s (`KONG_UPSTREAM_KEEPALIVE_IDLE_TIMEOUT`) | Whoever reuses a connection must close it first. With uvicorn's default 5 s, Kong sometimes sent a payment on a connection uvicorn was closing: bursts of 502/500 (`docs/results/scaling.md`). `tests/test_k8s_manifests.py` checks the order |
| Disruption budgets | `minAvailable: 1` for scoring, ingest, alert-svc | A node drain or upgrade keeps one copy of each running. Crashes are handled by the second copy and the probes |
| Data stores | unchanged: one copy each, readiness only | Single copies with node-local disks; a liveness restart of Cassandra mid-compaction would do more harm than good. Their failure tests are week 5 |

`make k8s-resilience TEST=scale` (also `kill`, `rollout`, `drain`) runs a test under real load.
