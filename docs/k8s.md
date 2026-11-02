# Kubernetes (k3d)

Manifests: `deploy/k8s/base/` (Kustomize). Deploy: `make cluster` then `make k8s-up`.
Gateway: `http://localhost:8080` (compose stays on 8000).

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

`make k8s-up` creates these with `kubectl create ... --dry-run=client -o yaml | kubectl apply`
(so it is safe to run again) because Kustomize can only read files inside `deploy/k8s/`.

## How images get in

There is no image registry. `make k8s-up` builds our 3 images with compose (names
`fraud/<service>:dev`, the same in compose and in the manifests), then `k3d image import` copies
them and the public images into every node. Our pods use `imagePullPolicy: Never`, so they can only
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
- Next (later weeks): NetworkPolicies (ST4), run as non-root, mTLS between services (ST3).
