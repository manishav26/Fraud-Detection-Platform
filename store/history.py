"""Per-user payment history in Cassandra (table fraud.txn_by_user, deploy/cassandra/schema.cql).

    store = HistoryStore()
    past = store.past(user_id, ts)     # this user's earlier payments, oldest first
    features = history_for_one(past, ...)   # features/history.py, same code as training
    store.save(txn)                    # after scoring, so the next payment sees this one

Saving the same txn_id at the same ts again overwrites the row (an "upsert"), so retries
never create duplicates. Design and reasons: docs/cassandra.md
"""

import os
from datetime import UTC, datetime

from cassandra.cluster import EXEC_PROFILE_DEFAULT, Cluster, ExecutionProfile
from cassandra.policies import DCAwareRoundRobinPolicy, TokenAwarePolicy
from cassandra.query import ConsistencyLevel, dict_factory

# history feature column name -> Cassandra column name
COLUMNS = {"P_emaildomain": "p_emaildomain", "DeviceType": "device_type",
           "ProductCD": "product_cd"}

INSERT = ("INSERT INTO txn_by_user (user_id, ts, txn_id, amount, p_emaildomain, device_type,"
          " product_cd) VALUES (?, ?, ?, ?, ?, ?, ?)")
# Strictly earlier than ts: a payment never counts itself in its own history.
SELECT = ("SELECT ts, amount, p_emaildomain, device_type, product_cd FROM txn_by_user"
          " WHERE user_id = ? AND ts < ?")


def to_datetime(ts):
    """ISO string or datetime -> datetime in UTC. Cassandra returns UTC without a timezone."""
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def row_values(txn):
    """A request dict (txn_id, user_id, amount, ts, attrs) -> values for INSERT, in order."""
    attrs = txn.get("attrs") or {}
    return (txn["user_id"], to_datetime(txn["ts"]), txn["txn_id"], float(txn["amount"]),
            *(attrs.get(col) for col in COLUMNS))


def to_past(rows):
    """Cassandra rows -> the `past` list that features.history.history_for_one expects."""
    return [{"t": to_datetime(r["ts"]).timestamp(), "amount": r["amount"],
             **{col: r[cass] for col, cass in COLUMNS.items()}} for r in rows]


class HistoryStore:
    def __init__(self, hosts=None, keyspace="fraud", consistency=None):
        hosts = hosts or os.getenv("CASSANDRA_HOSTS", "localhost").split(",")
        level = consistency or os.getenv("CASSANDRA_CONSISTENCY", "LOCAL_QUORUM")
        profile = ExecutionProfile(
            # send each query straight to a node that holds that user's partition
            load_balancing_policy=TokenAwarePolicy(
                DCAwareRoundRobinPolicy(local_dc=os.getenv("CASSANDRA_DC", "datacenter1"))),
            consistency_level=ConsistencyLevel.name_to_value[level],
            row_factory=dict_factory,
        )
        self.cluster = Cluster(hosts, execution_profiles={EXEC_PROFILE_DEFAULT: profile})
        self.session = self.cluster.connect(keyspace)
        # prepared once: Cassandra parses the query a single time, later calls only send values
        self.insert = self.session.prepare(INSERT)
        self.select = self.session.prepare(SELECT)

    def save(self, txn):
        self.session.execute(self.insert, row_values(txn))

    def past(self, user_id, ts):
        return to_past(self.session.execute(self.select, (user_id, to_datetime(ts))))

    def close(self):
        self.cluster.shutdown()
