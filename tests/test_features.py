"""Tests for the shared features package: hand-computed answers, no leaks, parity."""

import math

import numpy as np
import pandas as pd
import pytest

from features.history import HISTORY_FEATURES, NOVELTY_OF, add_history, history_for_one
from features.proxy_id import add_proxy_id
from features.request_time import hour_of_day, log_amount


def make(rows):
    """Build a small table. rows: (uid, t, amount, email); known users only."""
    df = pd.DataFrame(rows, columns=["uid", "TransactionDT", "TransactionAmt", "P_emaildomain"])
    df["DeviceType"] = "desktop"
    df["ProductCD"] = "W"
    df["uid_known"] = True
    return df


# One user, four transactions: t = 0, 30 min, 2 h, 25 h
SARA = [("sara", 0, 10.0, "a.com"), ("sara", 1800, 20.0, "a.com"),
        ("sara", 7200, 30.0, "a.com"), ("sara", 90000, 40.0, "b.com")]


def row(df, t):
    return df[df["TransactionDT"] == t].iloc[0]


# ---------- request-time features ----------

def test_log_amount():
    assert log_amount(0) == 0
    assert log_amount(np.e - 1) == pytest.approx(1.0)


def test_hour_of_day():
    assert list(hour_of_day([0, 3599, 3600, 86400 + 5 * 3600])) == [0, 0, 1, 5]


# ---------- proxy id ----------

def test_proxy_id_known_user():
    df = pd.DataFrame({"card1": [9500], "addr1": [204.0], "D1": [30.0],
                       "TransactionDT": [100 * 86400]})
    out = add_proxy_id(df)
    assert out["uid"][0] == "9500|204.0|70.0"
    assert bool(out["uid_known"][0])


def test_proxy_id_missing_addr_is_unknown():
    df = pd.DataFrame({"card1": [9500], "addr1": [np.nan], "D1": [30.0],
                       "TransactionDT": [100 * 86400]})
    out = add_proxy_id(df)
    assert out["uid"][0] == "9500|NA|70.0"
    assert not bool(out["uid_known"][0])


# ---------- hand-computed history values ----------

def test_first_transaction_has_empty_history():
    r = row(add_history(make(SARA)), 0)
    assert r["n_prev_txns"] == 0 and r["txn_count_24h"] == 0
    assert math.isnan(r["amt_mean_30d"]) and math.isnan(r["secs_since_last"])
    assert r["new_P_emaildomain"] == 0


def test_counts_in_windows():
    df = add_history(make(SARA))
    r2 = row(df, 7200)                       # earlier: t=0, t=1800
    assert r2["txn_count_1h"] == 0           # both are more than 1 h before
    assert r2["txn_count_24h"] == 2
    r3 = row(df, 90000)                      # earlier: 0, 1800, 7200
    assert r3["n_prev_txns"] == 3
    assert r3["txn_count_24h"] == 1          # only t=7200 is within 24 h
    assert r3["txn_count_30d"] == 3


def test_amount_sum_24h():
    assert row(add_history(make(SARA)), 90000)["amt_sum_24h"] == 30.0


def test_mean_std_zscore():
    r = row(add_history(make(SARA)), 90000)  # earlier amounts 10, 20, 30
    assert r["amt_mean_30d"] == pytest.approx(20.0)
    assert r["amt_std_30d"] == pytest.approx(np.sqrt(200 / 3))
    assert r["amount_zscore"] == pytest.approx((40 - 20) / np.sqrt(200 / 3))


def test_secs_since_last():
    assert row(add_history(make(SARA)), 90000)["secs_since_last"] == 90000 - 7200


def test_novelty_flag():
    df = add_history(make(SARA))
    assert row(df, 7200)["new_P_emaildomain"] == 0     # a.com seen before
    assert row(df, 90000)["new_P_emaildomain"] == 1    # b.com is new


# ---------- no leaks ----------

def test_future_rows_do_not_change_past_features():
    before = add_history(make(SARA))
    future = SARA + [("sara", 95000, 999.0, "z.com"), ("sara", 99000, 5.0, "a.com")]
    after = add_history(make(future))
    for t in [0, 1800, 7200, 90000]:
        a, b = row(before, t)[HISTORY_FEATURES], row(after, t)[HISTORY_FEATURES]
        assert np.allclose(a.astype(float), b.astype(float), equal_nan=True)


def test_other_users_do_not_leak_in():
    other = [("bob", 1000, 500.0, "x.com"), ("bob", 5000, 600.0, "x.com")]
    r = row(add_history(make(SARA + other)), 90000)
    assert r["n_prev_txns"] == 3 and r["amt_sum_24h"] == 30.0


def test_unknown_user_gets_no_history():
    df = make(SARA)
    df["uid_known"] = False
    out = add_history(df)
    assert out[HISTORY_FEATURES].isna().all().all()


def test_online_unknown_user():
    out = history_for_one([], 0, 10.0, {c: "x" for c in NOVELTY_OF}, known=False)
    assert all(math.isnan(v) for v in out.values())


# ---------- train/serve parity: offline == online ----------

def test_offline_matches_online_on_random_data():
    rng = np.random.default_rng(0)
    n = 300
    df = pd.DataFrame({
        "uid": rng.choice(["u1", "u2", "u3", "u4"], n),
        "TransactionDT": np.sort(rng.integers(0, 40 * 86400, n)),
        "TransactionAmt": rng.gamma(2, 50, n).round(2),
        "P_emaildomain": rng.choice(["a.com", "b.com", None], n),
        "DeviceType": rng.choice(["mobile", "desktop"], n),
        "ProductCD": rng.choice(["W", "C"], n),
    })
    df["uid_known"] = True
    offline = add_history(df.copy())
    history = {}
    for _, r in offline.iterrows():
        past = history.setdefault(r["uid"], [])
        values = {c: r[c] for c in NOVELTY_OF}
        online = history_for_one(past, r["TransactionDT"], r["TransactionAmt"], values)
        for name in HISTORY_FEATURES:
            assert online[name] == pytest.approx(r[name], nan_ok=True), name
        past.append({"t": r["TransactionDT"], "amount": r["TransactionAmt"], **values})


def test_same_amount_every_time_has_zero_spread_on_both_sides():
    # 40 equal payments: running sums of squares used to leave a tiny fake spread offline
    # (and a zscore in the millions) while live scoring got exactly 0
    rows = [("sam", i * 3600, 49.95, "a.com") for i in range(40)]
    offline = add_history(make(rows))
    past = []
    for _, r in offline.iterrows():
        online = history_for_one(past, r["TransactionDT"], r["TransactionAmt"],
                                 {c: r[c] for c in NOVELTY_OF})
        if r["n_prev_txns"] > 1:
            assert r["amt_std_30d"] == 0 and online["amt_std_30d"] == 0
            assert math.isnan(r["amount_zscore"]) and math.isnan(online["amount_zscore"])
        past.append({"t": r["TransactionDT"], "amount": r["TransactionAmt"],
                     **{c: r[c] for c in NOVELTY_OF}})