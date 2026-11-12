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
| Linkerd's webhooks: `webhookFailurePolicy=Fail` (`scripts/k8s_linkerd.sh`; `kube-system` exempt) | fail closed | while the proxy injector isn't answering, a pod waits instead of starting without its proxy. With Linkerd's default (`Ignore`), pods created during the VM's restart (12 Nov) ran unmeshed and could reach nothing. `kube-system` is exempt, as Linkerd advises, so a down injector can't also block CoreDNS. `make k8s-health` finds any pod without its proxy |
| `config.linkerd.io/default-inbound-policy` | `all-authenticated` | a pod accepts only callers that are in the mesh, so a pod without a mesh identity is refused even where the network rules let it through |
| `config.linkerd.io/opaque-ports` | 9042, 29092, 5432 | Cassandra, Redpanda and Postgres aren't HTTP: the proxies encrypt them without trying to read them |
| Kong's port 8000 (`mesh.yaml`) | `all-unauthenticated` | the public entrance: the laptop and payment clients aren't in the mesh. Kong still checks every token |
| Prometheus: no `skip-outbound-ports` | | it reads each proxy's metrics (4191) through its own proxy, with its mesh identity: `all-authenticated` covers the proxies' admin port too. Until 12 Nov it skipped its proxy on 4191 and every scrape was refused (below, "Seeing it") |
| `config.linkerd.io/proxy-cpu-request`, `proxy-memory-request`, `proxy-memory-limit` | 10m, 20Mi, 128Mi; no CPU limit | (week 5) without requests the scheduler didn't count the proxies, and scoring's HPA couldn't compute (`docs/k8s.md`). Measured use: 10-14 MiB, 3-10m CPU per proxy |

The settings are read when a pod starts. `make k8s-up` restarts our Deployments anyway, and
restarts a data store once if its pod has no proxy yet. The data stores' proxies get the resource
requests the next time their pods restart; until then only our services' proxies have them.

## How it fits with the NetworkPolicies

Between two meshed pods, a connection goes to the receiver's **proxy port, 4143**, not the app's
port; the receiving proxy then hands it to the app. A NetworkPolicy that allows only the app's
port therefore blocks all meshed traffic. So every allowed path is opened on both its app port
and 4143 (`scripts/gen_network_policies.py`), every pod may reach the Linkerd control plane (its
certificate, where to send traffic, which callers to accept), and Prometheus may read every
proxy's metrics (4191, through its own proxy). Port 4143 is the same for every service, so the network rules say which
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

**Both Mesh panels were empty until 12 Nov.** On the VM, Prometheus showed all 20 `linkerd-proxy`
targets down with `403 Forbidden`, and each proxy logged the scrape as refused:
```
Request denied server.name=all-authenticated ... client.tls=None(NoClientHello) client.ip=10.42.3.82
HTTP/1.1 request failed error=unauthorized request on route path="/metrics"
```
(10.42.3.82 was the Prometheus pod.) Prometheus had `skip-outbound-ports: 4191`, so it reached
each proxy's metrics port in plain text, with no mesh identity, and the namespace's
`all-authenticated` policy applies to the proxy's admin port as well. Its other scrapes already
went through its proxy, so skipping it bought nothing. Now it reads 4191 through its proxy, as
Linkerd's own Prometheus does: over mTLS, as the `prometheus` service account. No new policy: the
NetworkPolicies still let only Prometheus reach 4191, and the proxies' own probes (`/live`,
`/ready` on 4191) keep Linkerd's default handling. `tests/test_k8s_manifests.py` checks that no
pod skips its proxy on 4191.

Applied on the VM: 19 of 20 proxies answered at once, and ST3 still passed (outside the mesh 403,
the real ingest pod 200). The one still refused was Prometheus's own proxy, with
`client.ip` its own pod address and `client.tls=None`: a pod's call to its own address doesn't
get mTLS. Its proxy would count only Grafana's queries, not traffic between our services, so the
`linkerd-proxy` job leaves that one pod out (`deploy/prometheus/prometheus.k8s.yml`, tested)
rather than open its admin port to callers without an identity.

## Limits

- **A new pod in the namespace gets an identity automatically** (the namespace's `default`
  account), so the mesh alone wouldn't stop an attacker who can create pods here. The
  NetworkPolicies do: a pod without one of our `app` labels reaches nothing (ST4's intruder).
- **The certificates' trust anchor is created by `linkerd install` and lives in the cluster**, valid
  for a year. A real deployment would bring its own and rotate it.
- **Memory:** the control plane plus a proxy in every pod add roughly 0.5-0.7 GB on this laptop.
