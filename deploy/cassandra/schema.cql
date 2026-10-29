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
