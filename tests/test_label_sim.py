"""Tests for the label simulator: no label may be known before its release time."""

import numpy as np
import pandas as pd
import pytest

from ml.labels.label_sim import SECONDS_PER_DAY, known_labels, release_schedule, release_time

DAY = SECONDS_PER_DAY


def make(times):
    return pd.DataFrame({"TransactionID": range(len(times)), "TransactionDT": times,
                         "isFraud": [i % 2 for i in range(len(times))]})


def test_release_time_is_14_days_later():
    assert release_time(100, 14) == 100 + 14 * DAY


def test_negative_delay_is_rejected():
    with pytest.raises(ValueError):
        release_time(0, -1)


def test_label_not_known_one_second_early():
    df = make([10 * DAY])
    assert len(known_labels(df, now=24 * DAY - 1)) == 0
    assert len(known_labels(df, now=24 * DAY)) == 1


def test_delay_is_configurable():
    df = make([10 * DAY])
    assert len(known_labels(df, now=10 * DAY, delay_days=0)) == 1
    assert len(known_labels(df, now=16 * DAY, delay_days=7)) == 0
    assert len(known_labels(df, now=17 * DAY, delay_days=7)) == 1


def test_no_label_known_before_release_on_random_data():
    rng = np.random.default_rng(0)
    df = make(np.sort(rng.integers(0, 60 * DAY, 2000)))
    for now in rng.integers(0, 80 * DAY, 50):
        known = known_labels(df, now)
        assert (known["TransactionDT"] + 14 * DAY <= now).all()
        # and nothing that should be known is missing
        assert len(known) == int((df["TransactionDT"] + 14 * DAY <= now).sum())


def test_schedule_releases_each_label_once_and_late():
    rng = np.random.default_rng(1)
    df = make(np.sort(rng.integers(0, 30 * DAY, 500)))
    sched = release_schedule(df)
    assert len(sched) == len(df) and sched["TransactionID"].is_unique
    assert (sched["release_time"] - sched["txn_time"] == 14 * DAY).all()
    assert sched["release_time"].is_monotonic_increasing