# The measurement VM (week 5)

The laptop can't carry the whole platform under load: the scale test reached 37.9 of 80 payments
per second, Cassandra missed its read limit often enough to open scoring's breaker 12-14 times in
half an hour, and nodes went NotReady during deploys (`docs/results/scaling.md`). Week 5's
measurements (ramp, scaling, consistency, failure tests, the ST7 rerun, the platform run with
Manisha) run on one AWS VM instead, with the same k3d setup and the same scripts.

| Choice | Value | Why |
|---|---|---|
| Instance | m7i.2xlarge: 8 vCPU, 32 GiB, Intel, **on-demand** | the platform uses about 6 GB today; a 3-node Cassandra adds about 3.5 GB, the feature job 2 GB, plus the load generator; 4 scoring copies at once need the cores. Not spot: a spot VM can be taken away mid-test |
| Region | us-east-2 (Ohio) | the account's region: it uses AWS's simplified console, which sets the region itself (Oregon was the first choice). Nothing here depends on the region; the load, Cassandra and scoring all run inside the VM |
| OS | Ubuntu Server 24.04 LTS, x86_64 | `scripts/vm_setup.sh` installs the laptop's tool versions on it |
| Disk | 100 GB gp3, encrypted | images, data and Cassandra; encryption at rest for the measurement runs (threat 8) |
| Network | only SSH (22), only from your IP | everything else through SSH tunnels |
| Cost | about $0.40 an hour running, about $8 a month for the disk (check the AWS pricing page) | about 40 hours from Thursday to the demo: roughly $20-25. **Stop it when idle** |

**What never goes to the VM:** your age private key (the VM gets its own, added as a second
recipient), the promotion **private** key (promotions are signed on the laptop), and the repo's
GitHub credentials (the VM gets the committed code from `scripts/vm_sync.sh`).

## 1. AWS account, once (console, us-east-2)

Done on 11 Nov 2026. The account is on AWS's **Free plan** ($120 of credits, which cover the
plan's roughly $25). If the launch (step 2) refuses m7i.2xlarge because of the plan, upgrade to
the Paid plan (Billing and Cost Management); check on that page that unused credits carry over.
After an upgrade, the budget alert below is what catches spending past the credits.

1. **Budget:** Billing and Cost Management -> Budgets -> Create budget -> Cost budget, monthly,
   **$30**; alerts at 50%, 80% and 100% to your e-mail.
2. **Encrypted disks by default:** EC2 -> Settings -> Data protection and security -> EBS
   encryption -> Manage -> **Always encrypt new EBS volumes**.
3. **SSH key:** on the laptop, then EC2 -> Key pairs -> Actions -> Import key pair, name
   `fraud-vm`, paste the contents of `~/.ssh/fraud-vm.pub`:
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/fraud-vm -C fraud-vm
   ```
4. **Security group** `fraud-vm`: inbound **SSH, port 22, source "My IP"** and nothing else;
   outbound: all (to pull images and packages). "My IP" is only the address you are on: from
   another network (campus), add one more SSH rule with source "My IP" while there, and test
   `ssh fraud-vm 'uname -a'` before the day you need it (some campus networks block port 22).
   Never "Anywhere (0.0.0.0/0)".

## 2. Launch (Thursday morning)

EC2 -> Launch instance:
- name `fraud-vm`; AMI **Ubuntu Server 24.04 LTS (HVM), SSD, 64-bit (x86)**, from Canonical
- instance type **m7i.2xlarge**; key pair `fraud-vm`; security group `fraud-vm`; auto-assign a
  public IP
- storage: **100 GiB gp3**, encrypted (on by default after step 1.2)
- Advanced details: **Metadata version: V2 only (token required)**; no IAM role

Then, on the laptop, add to `~/.ssh/config` (the public IP is on the instance's page):
```
Host fraud-vm
  HostName <the VM's public IPv4>
  User ubuntu
  IdentityFile ~/.ssh/fraud-vm
  ServerAliveInterval 30
```

## 3. Set the VM up (about 10 minutes)

```bash
ssh fraud-vm 'bash -s' < scripts/vm_setup.sh
```
It prints the versions and, last, **the VM's age public key** (`age1...`). Copy it.

## 4. Let the VM open the secrets file (laptop)

In `.sops.yaml`, add the VM's key after yours, comma-separated:
`age: age1gnm8...d83mtd,age1<the VM's key>`. Then re-encrypt the file for both keys (needs your
key, which stays here) and commit:
```bash
SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt sops updatekeys -y deploy/k8s/secrets.enc.env
```
```bash
git commit -am "Let the measurement VM open the cluster secrets"
```
The passwords don't change: only who can decrypt them.

## 5. Send the code, data and keys (laptop; again after every commit you want there)

```bash
scripts/vm_sync.sh fraud-vm
```

## 6. Start the platform (on the VM, inside tmux)

Stop the laptop's cluster first: it frees the laptop, and its Kong port 8080 is the one the tunnel
below uses.
```bash
k3d cluster stop fraud
```
Then on the VM:
```bash
ssh fraud-vm
```
```bash
tmux new -s fraud
```
```bash
cd ~/fraud && source .venv/bin/activate
```
```bash
make cluster AGENTS=3 && OVERLAY=vm make k8s-up && make k8s-backfill
```
After this first deploy, `make k8s-up` and `make k8s-apply` take the setup from the running cluster
(its Cassandra says which it is), so `OVERLAY=vm` isn't needed again, and asking for the other
setup is refused. `OVERLAY=vm` gives Cassandra 3 nodes, one per k3d agent (`docs/cassandra.md`, "Three nodes"); they
join one after another, so `k8s-up` takes a few minutes longer. Check all three are up (`UN`) and
both keyspaces have 3 copies:
```bash
kubectl -n fraud exec cassandra-0 -c cassandra -- nodetool status
```
`tmux` keeps a long test running if the SSH connection drops (`tmux attach -t fraud` to get back).
If `k8s-up` stops with "Could not decrypt ... is your age key in ...", step 4 hasn't reached the
VM yet: rerun `scripts/vm_sync.sh`. Then check it the way the laptop was checked:
```bash
make test && make k8s-st2 && make k8s-kafka-check && make replay URL=http://localhost:8080 LIMIT=2000
```

## 7. Seeing it from the laptop

Kong (`make` targets with `--url http://localhost:8080` work from the laptop too):
```bash
ssh -N -L 8080:localhost:8080 fraud-vm
```
Grafana, the SIEM view or Prometheus: run `make k8s-grafana` (or `k8s-siem`, `k8s-prometheus`) on
the VM in a tmux window, then tunnel its port (3000, 8088, 9090):
```bash
ssh -N -L 3000:localhost:3000 fraud-vm
```

## 8. Promotions (signed on the laptop)

`make k8s-promote` needs the cluster's MLflow and Kong, and the signing key, which stays on the
laptop. So the laptop's kubectl is pointed at the VM's cluster through a tunnel, for this only:
1. On the VM: `k3d kubeconfig get fraud > ~/vm.kubeconfig`; note the port in its `server:` line.
2. On the laptop: `scp fraud-vm:vm.kubeconfig ~/.kube/fraud-vm && chmod 600 ~/.kube/fraud-vm`,
   and change its `server:` to `https://127.0.0.1:<that port>`.
3. Tunnel Kong and the API: `ssh -N -L 8080:localhost:8080 -L <port>:localhost:<port> fraud-vm`
4. Promote as usual, with that kubeconfig:
   ```bash
   KUBECONFIG=~/.kube/fraud-vm make k8s-promote VERSION=<n> REASON="<why>"
   ```
That kubeconfig is the cluster's admin login: keep it at mode 600 and delete it at teardown.

## 9. Every day

- **Stop the instance when idle** (EC2 -> Instance state -> Stop). The disk keeps everything, and
  k3d's containers come back when Docker starts. Its public IP changes on every start: update
  `HostName` in `~/.ssh/config`.
- After a start: `ssh fraud-vm`, then `make k8s-health` (next section).

## 9a. After a stop and start

Give the cluster 5-10 minutes (Cassandra's nodes rejoin one at a time), then:
```bash
cd ~/fraud && source .venv/bin/activate && make k8s-health
```
It only reads, and prints the command that fixes each problem, in the order to run them; run
it again until it says `healthy`. Two problems seen on 12 Nov:
- **A node's kubelet with a certificate for an old address.** Docker can give a k3d node a new
  address when the VM starts, and that node's kubelet keeps its certificate for the old one: logs,
  exec and port-forward to its pods fail (`x509: certificate is valid for ..., not ...`). Fix:
  `docker restart <that node>`, one node at a time (it gets a certificate for its new address).
- **Pods without their mesh proxy.** Pods created while Linkerd's injector was still starting
  came up without one and, by our mesh rules, could reach nothing. The injector now fails closed
  (`scripts/k8s_linkerd.sh`: such pods wait until it answers instead), so this should not recur;
  the check still finds any older ones, and the fix deletes them so they're recreated with it.

## 9b. Resizing (the scaling rerun, 11 Nov)

The scaling test filled the 8 cores (`docs/results/scaling.md`), so its rerun uses m7i.4xlarge
(16 vCPU, 64 GiB; about twice the hourly price: check the pricing page). The disk and the cluster
stay; only the machine changes. After the rerun, resize back to m7i.2xlarge the same way.
1. Stop the VM: EC2 -> Instances -> `fraud-vm` -> Instance state -> **Stop**; wait for **Stopped**.
2. Actions -> Instance settings -> **Change instance type** -> `m7i.4xlarge` -> Apply.
3. Instance state -> **Start**; copy the new public IPv4 into `HostName` in `~/.ssh/config`.
4. `ssh fraud-vm`, then `make k8s-health` until it says `healthy` (9a).

## 10. Teardown (after the demo and the last measurement)

1. EC2: terminate `fraud-vm` (its disk is deleted with it); delete the security group and the key
   pair.
2. Laptop: remove the VM's key from `.sops.yaml`, run
   `SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt sops updatekeys -y deploy/k8s/secrets.enc.env`,
   then **rotate every password** (`make k8s-secrets ROTATE=1`, then `make k8s-up` on the laptop):
   the VM could read the old ones. Commit.
3. Delete `~/.kube/fraud-vm`; check Billing that nothing is left running.

## Not covered here

- **The consistency test** (ONE vs QUORUM): `docs/results/consistency.md`, run with
  `make k8s-consistency` on this VM once the 3-node Cassandra is up.
- **The failure tests** (a node killed): their own design.
- **gen_keys on the VM:** with only the promotion public key there, `scripts/gen_keys.sh` keeps it
  (it used to make a new pair whenever the private key was missing, which would have replaced the
  laptop's public key and made every promotion fail).
