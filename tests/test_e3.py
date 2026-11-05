"""Tests for the E3 simulator pieces: label delay, review capacity, retrain policies."""

import numpy as np
import pandas as pd

from ml.experiments.e3_retraining import (
    DriftTriggered,
    Periodic,
    capacity_flags,
    training_rows,
)
from ml.labels.label_sim import SECONDS_PER_DAY


def payments(first_day, last_day, per_day=10):
    days = np.repeat(np.arange(first_day, last_day + 1), per_day)
    secs = np.tile(np.linspace(0, SECONDS_PER_DAY - 1, per_day).astype(int), last_day - first_day + 1)
    return pd.DataFrame({"TransactionDT": days * SECONDS_PER_DAY + secs, "day": days})


def test_retrain_never_sees_labels_that_are_not_released_yet():
    df = payments(1, 200)
    for day in [110, 140, 170]:
        rows = training_rows(df, day, delay_days=14, window_days=60)
        end_of_day = (day + 1) * SECONDS_PER_DAY - 1
        assert (rows["TransactionDT"] + 14 * SECONDS_PER_DAY <= end_of_day).all()
        assert rows["day"].max() == day - 14          # newest usable day, nothing later
        assert rows["day"].min() == day - 14 - 59     # exactly 60 labelled days


def test_capacity_flags_the_top_share_of_the_day():
    scores = np.arange(100) / 100
    flagged = capacity_flags(scores, capacity=0.02)
    assert flagged.sum() == 2 and flagged[-2:].all()
    assert capacity_flags([0.5], capacity=0.02).sum() == 1   # at least one review per day


def test_periodic_retrains_every_30_days_after_the_start():
    policy = Periodic(first_day=110, every=30)
    assert [d for d in range(110, 183) if policy.wants_retrain(d)] == [140, 170]


def test_drift_policy_retrains_after_a_jump_and_respects_the_cooldown():
    rng = np.random.default_rng(0)
    policy = DriftTriggered(cooldown=7, weekly_cap=1)
    policy.observe(124, rng.random(3000) < 0.3)       # 30% of fraud missed: stable
    assert not policy.wants_retrain(124)
    policy.observe(125, rng.random(3000) < 0.8)       # jumps to 80%: drift
    assert policy.alarms and policy.wants_retrain(125)
    policy.retrained(125)
    policy.observe(127, rng.random(3000) < 0.1)       # another change soon after
    assert not policy.wants_retrain(128)              # cooldown: no retrain within 7 days
    if policy.pending:
        assert policy.wants_retrain(133)              # allowed again after the cooldown


def test_drift_policy_stays_quiet_on_a_stable_stream():
    rng = np.random.default_rng(1)
    policy = DriftTriggered()
    for day in range(124, 183):
        policy.observe(day, rng.random(100) < 0.4)
        assert not policy.wants_retrain(day)