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
  `CASSANDRA_CONSISTENCY=ONE`.
- **Replication 1 locally** (one node). On a 3-node cluster set `'datacenter1': 3`.
- **No TTL yet:** `n_prev_txns` and the novelty flags look at a user's whole history, and the
  dataset is only 6 months. A real bank would expire old rows (for example after 1 year).
