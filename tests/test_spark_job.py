"""The feature job's moving parts that don't need Spark or Cassandra (week 4, step 1).

apply_group is exactly what Spark runs per user per micro-batch; batch_rows is what each batch
writes. The Python writer's process() must give the same rows from the same payments.
"""

import json
import math
from datetime import UTC, datetime

import pandas as pd

from features.history import HISTORY_FEATURES, NOVELTY_OF
from stream import python_writer, spark_job
from stream import state as st
from stream.sink import FEATURE_INSERT, parse

T0 = 1_512_086_400.0   # 2017-12-01T00:00:00Z
NOW = datetime(2026, 11, 9, tzinfo=UTC)


def payment(txn_id, user, dt, amount=10.0, offset=0, **attrs):
    return {"txn_id": txn_id, "user_id": user, "t": T0 + dt, "amount": amount,
            **{c: attrs.get(c) for c in NOVELTY_OF}, "partition": 0, "offset": offset,
            "kafka_t": NOW.timestamp() - 1.5}


def test_apply_group_orders_by_time_and_carries_state_across_batches():
    batch1 = pd.DataFrame([payment("b", "1|2|3", 600, offset=2), payment("a", "1|2|3", 0, offset=1)])
    state, out = spark_job.apply_group(None, batch1)
    assert list(out["txn_id"]) == ["a", "b"] and list(out["status"]) == [st.NEW, st.NEW]
    assert out.iloc[1]["txn_count_1h"] == 1 and out.iloc[1]["secs_since_last"] == 600
    # next micro-batch: state from the checkpoint, plus a redelivered payment
    batch2 = pd.DataFrame([payment("a", "1|2|3", 0), payment("c", "1|2|3", 7200, amount=30.0)])
    state, out = spark_job.apply_group(state, batch2)
    assert list(out["status"]) == [st.DUPLICATE, st.NEW]
    assert out.iloc[1]["n_prev_txns"] == 2 and out.iloc[1]["user_n_txns"] == 3
    assert json.loads(state)["n"] == 3


def test_apply_group_unknown_user_has_missing_features_and_no_summary():
    _, out = spark_job.apply_group(None, pd.DataFrame([payment("x", "1|NA|3", 0)]))
    row = out.iloc[0]
    assert row["status"] == st.UNKNOWN_USER and all(math.isnan(row[f]) for f in HISTORY_FEATURES)
    assert pd.isna(row["user_n_txns"])


def test_batch_rows_writes_features_and_one_summary_per_user():
    _, a = spark_job.apply_group(None, pd.DataFrame([payment("a", "1|2|3", 0),
                                                     payment("b", "1|2|3", 60, amount=5.0)]))
    _, b = spark_job.apply_group(None, pd.DataFrame([payment("x", "1|NA|3", 0)]))
    _, c = spark_job.apply_group(json.dumps({"n": 1, "last_t": T0, "seen": {k: [None] for k in NOVELTY_OF},
                                             "window": [[T0, 10.0, "z"]]}),
                                 pd.DataFrame([payment("z", "9|9|9", 0)]))   # a duplicate
    features, users, counts, delays = spark_job.batch_rows(pd.concat([a, b, c]), NOW)
    assert counts == {st.NEW: 2, st.UNKNOWN_USER: 1, st.DUPLICATE: 1}
    assert [f[2] for f in features] == ["a", "b", "x"]           # no row for the duplicate
    assert [(u[0], u[1], u[4]) for u in users] == [("1|2|3", 2, 15.0)]   # latest summary only
    assert all(abs(d - 1.5) < 1e-6 for d in delays)
    assert len(features[0]) == FEATURE_INSERT.count("?")


def test_python_writer_gives_the_same_features_as_spark():
    pays = [payment("a", "1|2|3", 0, P_emaildomain="gmail.com"), payment("x", "1|NA|3", 5),
            payment("b", "1|2|3", 600, P_emaildomain="yahoo.com"), payment("b", "1|2|3", 600)]
    msgs = [({k: p[k] for k in ("txn_id", "user_id", "t", "amount", *NOVELTY_OF)}, p["kafka_t"])
            for p in pays]
    py_features, py_users, py_counts, _ = python_writer.process({}, msgs)
    _, out = spark_job.apply_group(None, pd.DataFrame([p for p in pays if p["user_id"] == "1|2|3"]))
    _, unknown = spark_job.apply_group(None, pd.DataFrame([pays[1]]))
    sp_features, sp_users, sp_counts, _ = spark_job.batch_rows(pd.concat([out, unknown]), NOW)
    def strip(rows):   # sorted by txn_id, without the written_at column
        return [r[:-1] for r in sorted(rows, key=lambda r: r[2])]
    assert strip(py_features) == strip(sp_features)   # same rows except the written_at column
    assert py_counts == sp_counts and [u[:5] for u in py_users] == [u[:5] for u in sp_users]


def test_parse_reads_what_ingest_writes_and_rejects_the_rest():
    msg = parse(json.dumps({"txn_id": 7, "user_id": "1|2|3", "amount": 12.5,
                            "ts": "2017-12-01T00:00:00Z", "attrs": {"DeviceType": "mobile"}}))
    assert msg["txn_id"] == "7" and msg["t"] == T0 and msg["DeviceType"] == "mobile"
    assert msg["P_emaildomain"] is None
    for bad in (b"not json", json.dumps({"txn_id": "1"}), json.dumps({"txn_id": "1",
                "user_id": "u", "amount": 1, "ts": "yesterday"})):
        assert parse(bad) is None


def fake_sink(monkeypatch, fail_times):
    """A FeatureSink whose Cassandra fails each row's first `fail_times` writes."""
    import cassandra.concurrent

    from stream.sink import FeatureSink
    sink = object.__new__(FeatureSink)
    sink.session, sink.RETRY_WAITS = None, (0, 0, 0)
    tries, written = {}, []

    def execute(session, statement, rows, concurrency, raise_on_first_error):
        out = []
        for row in rows:
            tries[row] = tries.get(row, 0) + 1
            ok = tries[row] > fail_times.get(row, 0)
            if ok:
                written.append(row)
            out.append((ok, None if ok else TimeoutError(f"row {row}")))
        return out
    monkeypatch.setattr(cassandra.concurrent, "execute_concurrent_with_args", execute)
    return sink, tries, written


def test_writes_that_time_out_are_tried_again_and_only_those(monkeypatch):
    sink, tries, written = fake_sink(monkeypatch, {"b": 2})
    sink._write_all("stmt", ["a", "b", "c"], 32)
    assert sorted(written) == ["a", "b", "c"] and tries == {"a": 1, "b": 3, "c": 1}


def test_a_write_that_keeps_failing_stops_the_batch(monkeypatch):
    import pytest
    sink, tries, _ = fake_sink(monkeypatch, {"b": 99})
    with pytest.raises(TimeoutError):
        sink._write_all("stmt", ["a", "b"], 32)
    assert tries["b"] == 4 and tries["a"] == 1   # 1 try + 3 retries, then the batch fails


def test_unreadable_messages_are_counted_and_write_nothing():
    """What the Spark parse step sends for a message it can't read (a time Java can't parse, no
    txn_id, ...): grouped under UNREADABLE_KEY, counted, never written, never fatal."""
    bad = pd.DataFrame([{**payment(None, spark_job.UNREADABLE_KEY, 0), "t": float("nan")},
                        {**payment("bad-time", spark_job.UNREADABLE_KEY, 0), "t": float("nan")}])
    state, out = spark_job.apply_group(None, bad)
    assert list(out["status"]) == [st.UNREADABLE, st.UNREADABLE]
    assert json.loads(state) == json.loads(st.dumps(st.empty()))   # no user state for them
    _, good = spark_job.apply_group(None, pd.DataFrame([payment("a", "1|2|3", 0)]))
    features, users, counts, _ = spark_job.batch_rows(pd.concat([good, out]), NOW)
    assert counts == {st.NEW: 1, st.UNREADABLE: 2}
    assert [f[2] for f in features] == ["a"] and len(users) == 1


def test_the_unreadable_key_can_never_be_a_user_id():
    """ingest refuses any id with a character outside its pattern, and "~" is one."""
    import re

    from app.main import ID_PATTERN  # ingest-svc
    assert re.match(ID_PATTERN, "1|2|3") and not re.match(ID_PATTERN, spark_job.UNREADABLE_KEY)


def test_the_feature_job_writes_at_local_quorum(monkeypatch):
    """The same as scoring: with 3 copies a write counts once 2 nodes have it (docs/cassandra.md)."""
    import cassandra.cluster as cc

    from stream.sink import FeatureSink
    seen = {}

    class FakeCluster:
        def __init__(self, hosts, auth_provider, execution_profiles):
            seen["profile"] = execution_profiles[cc.EXEC_PROFILE_DEFAULT]

        def connect(self, keyspace):
            class Session:
                def prepare(self, query):
                    return query
            return Session()

    monkeypatch.setattr(cc, "Cluster", FakeCluster)
    monkeypatch.delenv("CASSANDRA_CONSISTENCY", raising=False)
    FeatureSink(hosts=["x"])
    from cassandra import ConsistencyLevel
    assert seen["profile"].consistency_level == ConsistencyLevel.LOCAL_QUORUM
