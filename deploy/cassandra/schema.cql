-- Keyspace and table for per-user history (week 2, step 2). Safe to run again: IF NOT EXISTS.
-- Design and reasons: docs/cassandra.md

-- 1 copy of each row on the single local node; use 3 on a 3-node cluster.
CREATE KEYSPACE IF NOT EXISTS fraud
  WITH replication = {'class': 'NetworkTopologyStrategy', 'datacenter1': 1};

-- One partition per user; rows inside it sorted by time, so "this user's earlier payments"
-- is one fast, in-order read. Columns are what features/history.py needs.
CREATE TABLE IF NOT EXISTS fraud.txn_by_user (
  user_id        text,
  ts             timestamp,
  txn_id         text,
  amount         double,
  p_emaildomain  text,
  device_type    text,
  product_cd     text,
  PRIMARY KEY ((user_id), ts, txn_id)
) WITH CLUSTERING ORDER BY (ts ASC, txn_id ASC);

-- Written by the streaming feature job (week 4, step 1; docs/spark.md), read by the parity check
-- and later by drift monitoring. Never read by scoring.

-- The 12 history features of every payment, as of that payment. One partition per day of
-- payment time, rows sorted by time: "a day's features" is one in-order read.
CREATE TABLE IF NOT EXISTS fraud.features_by_txn (
  day                date,
  ts                 timestamp,
  txn_id             text,
  user_id            text,
  n_prev_txns        double,
  txn_count_1h       double,
  txn_count_24h      double,
  txn_count_30d      double,
  amt_sum_24h        double,
  amt_mean_30d       double,
  amt_std_30d        double,
  amount_zscore      double,
  secs_since_last    double,
  new_p_emaildomain  double,
  new_devicetype     double,
  new_productcd      double,
  written_at         timestamp,   -- when the job wrote it: written_at - ts is the job's delay
  PRIMARY KEY ((day), ts, txn_id)
) WITH CLUSTERING ORDER BY (ts ASC, txn_id ASC);

-- The latest summary of each user's state: the job's per-user state, made visible.
CREATE TABLE IF NOT EXISTS fraud.user_features (
  user_id        text PRIMARY KEY,
  n_txns         int,
  last_ts        timestamp,
  txn_count_24h  int,
  amt_sum_24h    double,
  updated_at     timestamp
);
