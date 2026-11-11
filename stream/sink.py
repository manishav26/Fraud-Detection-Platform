"""Turning payments into feature rows, and writing them to Cassandra (week 4, step 1).

Shared by the Spark job and the Python writer: the same message parsing, the same rows, the same
writes. Design: docs/spark.md
"""

import json
import math
import os
from datetime import UTC, datetime

from features.history import HISTORY_FEATURES, NOVELTY_OF
from stream import state as st

# Cassandra folds unquoted names to lower case: new_P_emaildomain is stored as new_p_emaildomain
COLUMN = {f: f.lower() for f in HISTORY_FEATURES}
FEATURE_INSERT = (
    "INSERT INTO features_by_txn (day, ts, txn_id, user_id, "
    + ", ".join(COLUMN[f] for f in HISTORY_FEATURES)
    + ", written_at) VALUES (" + ", ".join(["?"] * (len(HISTORY_FEATURES) + 5)) + ")")
USER_INSERT = ("INSERT INTO user_features (user_id, n_txns, last_ts, txn_count_24h, amt_sum_24h,"
               " updated_at) VALUES (?, ?, ?, ?, ?, ?)")


def parse(value):
    """A txn.raw message (bytes or str, as ingest wrote it) -> the fields the state needs,
    or None if it can't be read (counted and skipped: one bad message must not stop the job)."""
    try:
        m = json.loads(value)
        attrs = m.get("attrs") or {}
        return {"txn_id": str(m["txn_id"]), "user_id": str(m["user_id"]),
                "t": st.to_epoch(m["ts"]), "amount": float(m["amount"]),
                **{c: attrs.get(c) for c in NOVELTY_OF}}
    except (ValueError, KeyError, TypeError):
        return None


def to_time(t):
    return datetime.fromtimestamp(t, UTC)


def _value(v):
    """A feature for INSERT; a missing one (e.g. seconds since the last payment, on a user's first)
    is left unset, not written as null: Cassandra keeps a null as a tombstone (store/history.py)."""
    from cassandra.query import UNSET_VALUE  # lazily, like the rest of this module's driver use

    return UNSET_VALUE if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)


def feature_params(txn_id, user_id, t, feats, written_at):
    ts = to_time(t)
    return (ts.date(), ts, txn_id, user_id, *(_value(feats[f]) for f in HISTORY_FEATURES),
            written_at)


def user_params(user_id, summary, written_at):
    return (user_id, int(summary["n_txns"]), to_time(summary["last_t"]),
            int(summary["txn_count_24h"]), float(summary["amt_sum_24h"]), written_at)


class FeatureSink:
    """Writes with the feature job's own login (`features`: may only write these two tables).
    Upserts: writing a row again gives the same row, so a replayed batch or a retried write is
    harmless.

    A slow moment in Cassandra (a 1 s memory-cleanup pause on this laptop) made a whole batch of
    10,000 writes time out, which stopped the Spark query and cost a restart. So writes that fail
    are tried again, only those, a few times with a growing wait; a batch fails only if Cassandra
    stays unavailable for about half a minute. Connecting at start waits for Cassandra too."""

    RETRY_WAITS = (1, 2, 4, 8, 15)   # seconds between attempts at the rows that failed

    def __init__(self, hosts=None, keyspace="fraud", attempts=30):
        import time

        from cassandra.auth import PlainTextAuthProvider
        from cassandra.cluster import (
            EXEC_PROFILE_DEFAULT,
            Cluster,
            ExecutionProfile,
            NoHostAvailable,
        )
        from cassandra.policies import DCAwareRoundRobinPolicy, TokenAwarePolicy
        from cassandra.query import ConsistencyLevel

        hosts = hosts or os.getenv("CASSANDRA_HOSTS", "localhost").split(",")
        auth = PlainTextAuthProvider(os.getenv("CASSANDRA_USER", "features"),
                                     os.getenv("CASSANDRA_PASSWORD", "features_dev_only"))
        # The same as scoring (store/history.py): LOCAL_QUORUM, so with 3 copies (the VM) a write
        # is on 2 nodes before it counts and the tables survive one node going down. The driver's
        # default (LOCAL_ONE) would count a write on one node only. Each write goes straight to a
        # node holding its partition.
        level = os.getenv("CASSANDRA_CONSISTENCY", "LOCAL_QUORUM")
        profile = ExecutionProfile(
            load_balancing_policy=TokenAwarePolicy(
                DCAwareRoundRobinPolicy(local_dc=os.getenv("CASSANDRA_DC", "datacenter1"))),
            consistency_level=ConsistencyLevel.name_to_value[level])
        for attempt in range(1, attempts + 1):
            self.cluster = Cluster(hosts, auth_provider=auth,
                                   execution_profiles={EXEC_PROFILE_DEFAULT: profile})
            try:
                self.session = self.cluster.connect(keyspace)
                break
            except NoHostAvailable:
                self.cluster.shutdown()
                if attempt == attempts:
                    raise
                print(f"Cassandra not reachable yet (attempt {attempt}), retrying", flush=True)
                time.sleep(2)
        self.feature_insert = self.session.prepare(FEATURE_INSERT)
        self.user_insert = self.session.prepare(USER_INSERT)

    def write(self, feature_rows, user_rows, concurrency=32):
        """feature_rows / user_rows: lists of parameter tuples (feature_params, user_params)."""
        for statement, rows in ((self.feature_insert, feature_rows), (self.user_insert, user_rows)):
            self._write_all(statement, rows, concurrency)

    def _write_all(self, statement, rows, concurrency):
        import time

        from cassandra.concurrent import execute_concurrent_with_args

        from stream import metrics as m

        for wait in (*self.RETRY_WAITS, None):
            if not rows:
                return
            results = execute_concurrent_with_args(self.session, statement, rows,
                                                   concurrency=concurrency,
                                                   raise_on_first_error=False)
            failed = [(row, result) for row, (ok, result) in zip(rows, results) if not ok]
            if not failed:
                return
            if wait is None:
                raise failed[0][1]   # still failing: stop, and resume from the checkpoint
            m.WRITE_RETRIES.inc(len(failed))
            print(f"{len(failed)} of {len(rows)} writes failed ({type(failed[0][1]).__name__}); "
                  f"retrying them in {wait} s", flush=True)
            time.sleep(wait)
            rows = [row for row, _ in failed]

    def close(self):
        self.cluster.shutdown()
