import uuid
from datetime import UTC, datetime

import pytest
from cassandra.cluster import NoHostAvailable

from features.history import NOVELTY_OF, history_for_one
from store.history import COLUMNS, HistoryStore, row_values, to_datetime, to_past


def txn(i, ts, user="u1", email="gmail.com"):
    return {"txn_id": f"t{i}", "user_id": user, "amount": 10.0 * i, "ts": ts,
            "attrs": {"P_emaildomain": email, "DeviceType": "mobile", "ProductCD": "W"}}


def test_columns_cover_novelty_features():
    assert set(COLUMNS) == set(NOVELTY_OF)


def test_to_datetime_is_utc():
    want = datetime(2026, 1, 1, 10, tzinfo=UTC)
    assert to_datetime("2026-01-01T10:00:00Z") == want
    assert to_datetime("2026-01-01T12:00:00+02:00") == want
    assert to_datetime(datetime(2026, 1, 1, 10)) == want   # Cassandra gives no timezone


def test_row_values_order_and_missing_attrs():
    values = row_values({"txn_id": "t1", "user_id": "u1", "amount": 5, "ts": "2026-01-01T00:00:00Z"})
    assert values == ("u1", datetime(2026, 1, 1, tzinfo=UTC), "t1", 5.0, None, None, None)


def test_to_past_feeds_history_features():
    rows = [{"ts": datetime(2026, 1, 1, 10), "amount": 20.0, "p_emaildomain": "gmail.com",
             "device_type": "mobile", "product_cd": "W"}]
    past = to_past(rows)
    t = past[0]["t"] + 600   # 10 minutes later
    out = history_for_one(past, t, 30.0, {"P_emaildomain": "yahoo.com", "DeviceType": "mobile",
                                          "ProductCD": "W"})
    assert out["txn_count_1h"] == 1 and out["secs_since_last"] == 600
    assert out["new_P_emaildomain"] == 1 and out["new_DeviceType"] == 0


# ---------- against a running Cassandra (`make up` + `make schema`); skipped otherwise ----------

@pytest.fixture(scope="module")
def store():
    try:
        s = HistoryStore()
    except NoHostAvailable:
        pytest.skip("Cassandra not running")
    yield s
    s.close()


def test_past_is_earlier_only_in_order_and_upserts(store):
    user = f"pytest-{uuid.uuid4()}"
    times = ["2026-01-01T10:00:00Z", "2026-01-01T11:00:00Z", "2026-01-01T12:00:00Z"]
    for i, ts in enumerate(times):
        store.save(txn(i, ts, user))
    store.save(txn(0, times[0], user))   # a retry of the first payment

    past = store.past(user, times[2])
    assert [p["amount"] for p in past] == [0.0, 10.0]   # no duplicate, not the payment itself

    store.session.execute("DELETE FROM txn_by_user WHERE user_id = %s", (user,))
