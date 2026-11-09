"""The streaming feature state gives exactly what Manisha's reference gives (week 4, step 1).

stream/state.py keeps only 30 days of payments plus a few counters; history_for_one sees the
user's whole history. On random histories the 12 features must be identical, and the same as the
offline training code (add_history) on a whole table.
"""

import math
import random

import numpy as np
import pandas as pd
import pytest

from features.history import HISTORY_FEATURES, NOVELTY_OF, add_history, history_for_one
from stream import state as st

DAY = 86400
EMAILS = ["gmail.com", "yahoo.com", "anonymous.com", None]
DEVICES = ["desktop", "mobile", None]
PRODUCTS = ["W", "C", "H"]


def same(a, b):
    if math.isnan(a) and math.isnan(b):
        return True
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)


def random_history(rng, n):
    """n payments for one user over up to 120 days, in time order, some in the same second."""
    t, rows = 1_700_000_000.0, []
    for i in range(n):
        t += rng.choice([0, 5, 600, 3600, DAY, 3 * DAY, 20 * DAY, 40 * DAY])
        rows.append({"txn_id": f"t{i}", "t": t, "amount": round(rng.uniform(1, 500), 2),
                     "P_emaildomain": rng.choice(EMAILS), "DeviceType": rng.choice(DEVICES),
                     "ProductCD": rng.choice(PRODUCTS)})
    return rows


@pytest.mark.parametrize("seed", range(40))
def test_state_matches_history_for_one_with_the_full_past(seed):
    rng = random.Random(seed)
    rows = random_history(rng, rng.randint(1, 60))
    s = st.empty()
    for i, r in enumerate(rows):
        values = {c: r[c] for c in NOVELTY_OF}
        status, got, s = st.update(s, r["txn_id"], "1|2|3", r["t"], r["amount"], values)
        want = history_for_one(rows[:i], r["t"], r["amount"], values)
        assert status == st.NEW
        bad = [f for f in HISTORY_FEATURES if not same(got[f], want[f])]
        assert not bad, f"payment {i}: {[(f, got[f], want[f]) for f in bad]}"
        # and the state stays small: nothing older than 30 days before the latest payment
        assert all(wt >= s["last_t"] - st.WINDOW_SECONDS for wt, _, _ in s["window"])
    st.loads(st.dumps(s))   # survives the JSON round trip


def test_state_matches_the_offline_training_code_on_a_table():
    rng = random.Random(7)
    rows, base = [], 1_700_000_000
    for user in range(30):
        uid = f"{user}|204|12" if user % 7 else f"{user}|NA|12"   # some unknown users
        for r in random_history(rng, rng.randint(1, 25)):
            rows.append({"uid": uid, "uid_known": "NA" not in uid,
                         "TransactionDT": int(r["t"]) - base, "TransactionAmt": r["amount"],
                         "TransactionID": len(rows), **{c: r[c] for c in NOVELTY_OF}})
    df = pd.DataFrame(rows).sort_values("TransactionDT", kind="stable")
    offline = add_history(df.copy()).set_index("TransactionID")

    states = {}
    for r in df.to_dict("records"):   # the stream: time order across all users
        values = {c: r[c] for c in NOVELTY_OF}
        s = states.get(r["uid"], st.empty())
        _, got, states[r["uid"]] = st.update(s, str(r["TransactionID"]), r["uid"],
                                             float(r["TransactionDT"] + base),
                                             r["TransactionAmt"], values)
        want = offline.loc[r["TransactionID"]]
        bad = [f for f in HISTORY_FEATURES if not same(got[f], float(want[f]))]
        assert not bad, (r["TransactionID"], [(f, got[f], want[f]) for f in bad])


def test_unknown_users_get_no_history_and_no_state():
    s = st.empty()
    status, got, s2 = st.update(s, "t1", "1|NA|12", 1e9, 10.0, {})
    assert status == st.UNKNOWN_USER and all(np.isnan(v) for v in got.values()) and s2 is s


def test_a_payment_sent_twice_is_counted_once():
    s = st.empty()
    _, _, s = st.update(s, "t1", "1|2|3", 1e9, 10.0, {})
    status, got, s2 = st.update(s, "t1", "1|2|3", 1e9, 10.0, {})
    assert status == st.DUPLICATE and got is None and s2 is s and s["n"] == 1


def test_a_payment_older_than_the_window_is_dropped():
    s = st.empty()
    _, _, s = st.update(s, "t1", "1|2|3", 1e9, 10.0, {})
    status, _, s2 = st.update(s, "t0", "1|2|3", 1e9 - 31 * DAY, 10.0, {})
    assert status == st.LATE and s2 is s


def test_a_slightly_late_payment_does_not_see_later_ones_as_past():
    s = st.empty()
    _, _, s = st.update(s, "t2", "1|2|3", 1e9 + 600, 50.0, {})   # arrives first, happens later
    _, got, s = st.update(s, "t1", "1|2|3", 1e9, 10.0, {})
    assert got["txn_count_1h"] == 0 and [r[2] for r in s["window"]] == ["t1", "t2"]


def test_new_value_features_remember_beyond_30_days():
    s = st.empty()
    _, _, s = st.update(s, "t1", "1|2|3", 1e9, 10.0, {"DeviceType": "mobile"})
    _, got, s = st.update(s, "t2", "1|2|3", 1e9 + 90 * DAY, 10.0, {"DeviceType": "mobile"})
    assert got["new_DeviceType"] == 0.0 and got["txn_count_30d"] == 0 and got["n_prev_txns"] == 1
    assert s["window"] == [[1e9 + 90 * DAY, 10.0, "t2"]]   # the old payment left the window


def test_summary_for_user_features():
    s = st.empty()
    for i, (dt, amt) in enumerate([(0, 10.0), (3600, 20.0), (2 * DAY, 5.0)]):
        _, _, s = st.update(s, f"t{i}", "1|2|3", 1e9 + dt, amt, {})
    assert st.summary(s, 1e9 + 2 * DAY) == {"n_txns": 3, "last_t": 1e9 + 2 * DAY,
                                           "txn_count_24h": 1, "amt_sum_24h": 5.0}


@pytest.mark.parametrize("ts, epoch", [("2017-12-01T00:00:00Z", 1512086400.0),
                                       ("2017-12-01T00:00:00+00:00", 1512086400.0)])
def test_timestamps_as_ingest_stores_them(ts, epoch):
    assert st.to_epoch(ts) == epoch
