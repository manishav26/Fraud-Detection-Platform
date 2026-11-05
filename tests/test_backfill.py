import math
from datetime import UTC, datetime

import pandas as pd

from features.history import NOVELTY_OF
from load.replay import to_ts
from store.backfill import READ, known_rows, to_values, verify
from store.history import COLUMNS, is_known, to_datetime


def past_payments():
    return pd.DataFrame({
        "TransactionID": [1, 2, 3],
        "TransactionDT": [86_400, 86_460, 90_000],
        "TransactionAmt": [10.0, 20.0, 30.0],
        "card1": [9500, 9500, 7000],
        "addr1": [204.0, 204.0, math.nan],    # the third has no address: an unknown user
        "D1": [1.0, 1.0, 1.0],
        "P_emaildomain": ["gmail.com", math.nan, "yahoo.com"],
        "DeviceType": ["mobile", "mobile", math.nan],
        "ProductCD": ["W", "W", "C"],
    })


class FakeStore:
    def __init__(self, counts):
        self.counts = counts
        self.asked_until = None

    def past(self, user, ts):
        self.asked_until = ts
        return [None] * self.counts[user]


def test_reads_every_column_history_needs():
    assert set(COLUMNS) == set(NOVELTY_OF) and set(COLUMNS) <= set(READ)


def test_unknown_rule():
    assert is_known("9500|204.0|0.0") and is_known("u5")
    assert not is_known("9500|NA|0.0")


def test_unknown_users_are_skipped():
    df = known_rows(past_payments())
    assert list(df["TransactionID"]) == [1, 2]
    assert df["uid"].map(is_known).all()


def test_values_use_the_same_timeline_as_replay_and_null_for_missing():
    row = known_rows(past_payments()).iloc[1].to_dict()
    user, ts, txn_id, amount, email, device, product = to_values(row)
    assert ts == to_datetime(to_ts(86_460))
    assert ts == datetime(2017, 12, 2, 0, 1, tzinfo=UTC)
    assert txn_id == "2" and amount == 20.0 and user == row["uid"]
    assert email is None and device == "mobile" and product == "W"


def test_verify_finds_a_user_with_missing_rows():
    df = known_rows(past_payments())
    user = df["uid"].iloc[0]
    assert verify(FakeStore({user: 2}), df) == []
    assert verify(FakeStore({user: 1}), df) == [(user, 2, 1)]


def test_verify_stops_at_the_end_of_the_data():
    df = known_rows(past_payments())
    store = FakeStore({df["uid"].iloc[0]: 2})
    verify(store, df)
    assert store.asked_until == to_ts(86_461)   # later replay rows must not count
