# mTLS between services (week 4, step 4)

Threat 3: someone inside the cluster network reads or changes traffic between services, or calls
a service while pretending to be another. Control: Linkerd. Every pod gets a proxy; connections
between pods are encrypted, and both ends prove who they are with a certificate tied to their
service account (step 3 gave each workload its own). Test: ST3 (`make k8s-st3`). Cluster only:
compose has no mesh.

Files: `deploy/k8s/base/namespace.yaml` (the settings), `deploy/k8s/base/mesh.yaml` (Kong's
exception), `scripts/k8s_linkerd.sh` (install), `scripts/gen_network_policies.py` (the
NetworkPolicies, now with the mesh's ports).

## Install (once)

1. The CLI, on the laptop (Linkerd's open-source releases are "edge" releases):
   ```bash
   curl --proto '=https' --tlsv1.2 -sSfL https://run.linkerd.io/install-edge | sh
   export PATH=$HOME/.linkerd2/bin:$PATH
   ```
2. `make k8s-up` then runs `scripts/k8s_linkerd.sh`: if the `linkerd` namespace is missing it
   installs Linkerd's CRDs and control plane, then runs `linkerd check`. The Gateway API CRDs
   Linkerd needs are already in the cluster (v1.4.0, from k3s's Traefik; Linkerd supports
   1.2.1-1.5.1).

## Settings (namespace `fraud`)

| Setting | Value | Why |
|---|---|---|
| `linkerd.io/inject` | `enabled` | every pod gets a proxy, including the data stores |
| `config.linkerd.io/default-inbound-policy` | `all-authenticated` | a pod accepts only callers that are in the mesh, so a pod without a mesh identity is refused even where the network rules let it through |
| `config.linkerd.io/opaque-ports` | 9042, 29092, 5432 | Cassandra, Redpanda and Postgres aren't HTTP: the proxies encrypt them without trying to read them |
| Kong's port 8000 (`mesh.yaml`) | `all-unauthenticated` | the public entrance: the laptop and payment clients aren't in the mesh. Kong still checks every token |
| Prometheus: `skip-outbound-ports: 4191` | | it reads each proxy's metrics directly, so measuring the mesh doesn't depend on it |

The settings are read when a pod starts. `make k8s-up` restarts our Deployments anyway, and
restarts a data store once if its pod has no proxy yet.

## How it fits with the NetworkPolicies

Between two meshed pods, a connection goes to the receiver's **proxy port, 4143**, not the app's
port; the receiving proxy then hands it to the app. A NetworkPolicy that allows only the app's
port therefore blocks all meshed traffic. So every allowed path is opened on both its app port
and 4143 (`scripts/gen_network_policies.py`), every pod may reach the Linkerd control plane (its
certificate, where to send traffic, which callers to accept), and Prometheus may read every
proxy's metrics (4191). Port 4143 is the same for every service, so the network rules say which
pods may talk, and the mesh says which identities may.

**So per-port rules move into the mesh.** Since every meshed connection arrives on 4143, a
NetworkPolicy can't keep ingest off Redpanda's metrics port (9644) while letting it use 29092: the
first ST4 run with the mesh found exactly that. Where one pod's ports have different callers
(Redpanda: 29092 for our services, 9644 for Prometheus; Kong: 8100 for Prometheus), the mesh
checks the caller's identity per port: a Linkerd `Server` for the port with `accessPolicy: deny`,
and an `AuthorizationPolicy` allowing only the service accounts in the paths table
(`deploy/k8s/base/mesh-ports.yaml`, generated with the NetworkPolicies). Pods with one port
need none: the NetworkPolicy already says who may reach them.

**This changed how ST4 checks a path.** A meshed pod's outgoing connection first reaches its own
proxy, which accepts it immediately, even if the far end is blocked. So ST4 now checks for a real
answer: on an HTTP port, an HTTP response that isn't the proxy's own error; on a database port,
the connection staying open (blocked: the proxy closes it).

## ST3

A test pod **labelled as ingest** (so the network rules let it reach scoring) but **without a
proxy** (so it has no mesh identity) sends scoring the same call ingest makes, `POST /score`. It
must be refused. The same call from a real ingest pod must work. That separates the two layers:
the network lets the packet through, the mesh refuses the caller. The test pod is never ready, so
Kong never sends it payments, and it is deleted afterwards.

## Seeing it

- `linkerd check` (installation and certificates healthy).
- Grafana, "Mesh: new connections per second, encrypted or not": mTLS connections by service.
  The only unencrypted ones should be Kubernetes' probes and Kong's public port.
- Grafana, "Mesh: callers refused by policy": ST3's refusals show up here.
- `linkerd viz edges` would show the same per pair, but the viz extension needs about 250 MB
  more on this laptop, so it isn't installed.

## Limits

- **A new pod in the namespace gets an identity automatically** (the namespace's `default`
  account), so the mesh alone wouldn't stop an attacker who can create pods here. The
  NetworkPolicies do: a pod without one of our `app` labels reaches nothing (ST4's intruder).
- **The certificates' trust anchor is created by `linkerd install` and lives in the cluster**, valid
  for a year. A real deployment would bring its own and rotate it.
- **Memory:** the control plane plus a proxy in every pod add roughly 0.5-0.7 GB on this laptop.
