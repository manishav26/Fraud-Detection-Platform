# Cassandra: per-user history

Created by `make schema` (`deploy/cassandra/schema.cql`). Code: `store/history.py`.
Speed test: `make cassandra-speed`, results in `docs/results/cassandra.md`.

## Table `fraud.txn_by_user`

| Column | Type | Role |
|---|---|---|
| `user_id` | text | **Partition key**: all of one user's rows live together on the same node(s) |
| `ts` | timestamp | **Clustering key 1**: rows inside the partition are sorted by time |
| `txn_id` | text | **Clustering key 2**: two payments at the same second stay separate rows |
| `amount` | double | for counts, sums, mean, std, z-score |
| `p_emaildomain`, `device_type`, `product_cd` | text | for the "is this new for the user" flags |

How it is used, per payment:

1. scoring-svc reads `WHERE user_id = ? AND ts < ?`: one partition, already in time order.
   `<` (not `<=`) means a payment never counts itself.
2. `features/history.py` `history_for_one` turns those rows into the 12 history features. It is
   the same code Manisha's training uses, so live and training features match.
3. After scoring, the payment is written with `INSERT`.

## Backfill

Replay starts at day 110. Without earlier history, a customer with 40 past payments in training
looks new live. `make backfill` (`store/backfill.py`) loads days 1-109 of known users.

- **Straight to Cassandra, not through the gateway:** old payments must not be scored or appear in
  `txn.raw` as new ones.
- **Same user ids and timeline as replay**, using the same functions, and the same "known user" rule
  as scoring, so old and new payments connect.
- **Safe to run again:** upserts. An interrupted run can simply be restarted.
- **Checked per user:** it compares stored and expected counts for 1,000 random users. A full
  `COUNT(*)` scans the table and wouldn't show one user's rows being wrong.
- **Run it when no load test is running:** it writes as fast as Cassandra allows.

## Why these choices

- **Query-first design:** in Cassandra you design the table around the one question you ask.
  Ours is "this user's earlier payments", so the user is the partition and time is the order.
- **One table, not a `user_features` table too:** the features depend on the *new* payment's time
  and amount (for example "payments in the hour before this one"), so they can't be stored ahead
  of time. We store the raw history and compute features at request time. The plan's
  `user_features` table is not needed.
- **Upserts:** in Cassandra an `INSERT` with an existing key overwrites the row. Our key is
  (user, time, txn id), so a retried payment replaces itself instead of being counted twice.
- **Prepared statements:** Cassandra parses each query once; later calls send only the values.
  Faster, and values are never pasted into query text, so no CQL injection.
- **Token-aware routing:** the driver sends each query straight to a node that owns that user.
- **Consistency `LOCAL_QUORUM` for reads and writes:** a majority of copies must answer. With 3
  copies that is 2 for writes + 2 for reads > 3, so every read sees the latest write, and one
  node can be down. `ONE` is faster but a read may miss a just-written payment. Change it with
  `CASSANDRA_CONSISTENCY=ONE`. The two are compared on the VM: `docs/results/consistency.md`.
- **Replication follows the cluster:** 1 copy on one node (compose, the laptop), 3 on the VM's 3
  nodes ("Three nodes" below). `scripts/create_schema.sh` sets it on every run (`CASSANDRA_RF`).
- **No TTL yet:** `n_prev_txns` and the novelty flags look at a user's whole history, and the
  dataset is only 6 months. A real bank would expire old rows (for example after 1 year).

## Logins (week 4, step 3)

Until week 4, Cassandra had no login: anything that reached port 9042 could read, change or drop
everything. Now it checks a password and a permission for every statement.

| Login | Used by | May |
|---|---|---|
| `scoring` | scoring-svc (k8s secret `cassandra-credentials`; compose env) | read and write `fraud.txn_by_user`, nothing else |
| `fraud_admin` | `scripts/create_schema.sh` and the laptop tools (backfill, read timing, parity) | everything (superuser) |
| `cassandra` (built in) | nobody | can't log in: its password is known to everyone, so the first run gives it a random one and turns its login off |

- **How it is switched on:** the image has no setting for it, so the container edits its config
  (`authenticator: PasswordAuthenticator`, `authorizer: CassandraAuthorizer`) and then starts
  as usual (`deploy/k8s/base/cassandra.yaml`, `compose.yaml`).
- **Clients:** `store/history.py` logs in with `CASSANDRA_USER` and `CASSANDRA_PASSWORD`,
  defaulting to `fraud_admin` for the laptop tools.
- **The limit Cassandra can't go below:** its `MODIFY` permission covers INSERT, UPDATE, DELETE
  and TRUNCATE together, so scoring could still empty its own table. It can't touch any other
  table, change the schema, or read the logins.
- **Test:** `make k8s-st4-db` (`security/tests/st4_cassandra_logins.sh`). Dev passwords, like
  Postgres's; encrypted secrets are step 5.

## Three nodes (week 5, the measurement VM)

The consistency (ONE vs QUORUM) and failure tests need more than one copy of the data: with one
node there is nothing to fail over to and no second copy to disagree with. The laptop can't hold
three, so they run only on the VM (`docs/vm.md`): `OVERLAY=vm make k8s-up` applies
`deploy/k8s/vm`, the base plus these changes.

| Choice | Design | Why |
|---|---|---|
| Nodes | 3, started one at a time (`OrderedReady`) | Cassandra refuses to bootstrap two nodes at once |
| Ready when | `nodetool statusbinary` says `running`, which is after the node has joined | the base's TCP check on 9042 is answered by the Linkerd proxy before Cassandra is up, so the next node could start too early |
| One per machine | required anti-affinity across the k3d nodes (`make cluster AGENTS=3`) | "a node fails" is a whole k3d node stopping, and loses one copy, never two |
| Finding each other | headless Service `cassandra-peers` (`cassandra-0.cassandra-peers...` as the seed); clients keep the `cassandra` Service | Cassandra's standard way of joining; the driver learns all three and routes each read to a node holding that card |
| Copies | `fraud` and `system_auth` at 3 (`CASSANDRA_RF=3`), then `nodetool repair -full system_auth` | Cassandra keeps logins at 1 copy by default: losing that node would fail every login. `create_schema.sh` refuses more copies than nodes up |
| Consistency | `LOCAL_QUORUM` (2 of 3) for scoring and, since week 5, the feature job (it used the driver's `LOCAL_ONE`) | every read sees the latest write, and one node can be down |
| Drains | PodDisruptionBudget, at most 1 node | a drain never takes two copies |
| Node to node | port 7000, mTLS through Linkerd (opaque), only Cassandra's own identity may call it; a NetworkPolicy rule cassandra -> cassandra | the platform's rule: every connection encrypted and both ends proven |

Raising the copies of a keyspace that already holds data needs `nodetool repair -full fraud` once,
so the existing rows reach the new replicas; on the VM the keyspace is created before any data.

