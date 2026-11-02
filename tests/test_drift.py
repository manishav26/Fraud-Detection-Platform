"""Tests for the drift detectors: PSI, KS and ADWIN."""

import numpy as np
import pandas as pd
import pytest

from ml.drift.detectors import ADWIN, feature_drift, ks, level, psi, psi_categorical

rng = np.random.default_rng(0)


def test_psi_is_near_zero_for_the_same_distribution():
    a, b = rng.normal(0, 1, 20000), rng.normal(0, 1, 20000)
    assert psi(a, b) < 0.01
    assert level(psi(a, b)) == "stable"


def test_psi_flags_a_shift():
    a = rng.normal(0, 1, 20000)
    assert level(psi(a, rng.normal(0.4, 1, 20000))) == "moderate"
    assert level(psi(a, rng.normal(1.0, 1, 20000))) == "major"


def test_psi_counts_values_outside_the_old_range_and_missing():
    a = rng.uniform(0, 1, 5000)
    assert psi(a, rng.uniform(5, 6, 5000)) > 1          # all new values above the old max
    with_missing = np.where(rng.random(5000) < 0.5, np.nan, a)
    assert psi(a, with_missing) > 0.2                   # half suddenly missing


def test_psi_handles_a_constant_feature():
    assert psi(np.ones(100), np.ones(100)) == pytest.approx(0, abs=1e-9)


def test_categorical_psi_sees_new_and_vanished_values():
    old = ["W"] * 70 + ["C"] * 30
    assert psi_categorical(old, old) == pytest.approx(0, abs=1e-9)
    assert psi_categorical(old, ["W"] * 70 + ["NEW"] * 30) > 1


def test_ks_detects_shift_and_ignores_missing():
    a = rng.normal(0, 1, 3000)
    stat, p = ks(a, a)
    assert stat == 0 and p == pytest.approx(1)
    stat, p = ks(a, np.append(rng.normal(1, 1, 3000), [np.nan] * 10))
    assert stat > 0.3 and p < 1e-6


def test_feature_drift_table_is_sorted_worst_first():
    ref = pd.DataFrame({"same": rng.normal(0, 1, 5000), "moved": rng.normal(0, 1, 5000),
                        "cat": rng.choice(["a", "b"], 5000)})
    cur = pd.DataFrame({"same": rng.normal(0, 1, 5000), "moved": rng.normal(2, 1, 5000),
                        "cat": rng.choice(["a", "b"], 5000)})
    table = feature_drift(ref, cur, ["same", "moved", "cat"], categorical=["cat"])
    assert table["feature"].iloc[0] == "moved"
    assert table.set_index("feature").loc["moved", "level"] == "major"
    assert np.isnan(table.set_index("feature").loc["cat", "KS statistic"])


def test_adwin_stays_quiet_on_a_stable_stream():
    detector = ADWIN()
    for x in rng.random(20000) < 0.1:          # 10% error rate, never changes
        detector.update(x)
    assert detector.drifts == []


def test_adwin_detects_a_jump_quickly_and_forgets_the_old_data():
    detector = ADWIN()
    for x in rng.random(3000) < 0.1:
        detector.update(x)
    for x in rng.random(3000) < 0.4:           # error rate jumps from 10% to 40%
        detector.update(x)
    assert detector.drifts, "no drift detected"
    assert 3000 < detector.drifts[0] < 3300    # within 300 values of the change
    assert detector.mean == pytest.approx(0.4, abs=0.05)   # window now holds only new data


def test_adwin_window_is_capped():
    detector = ADWIN(max_window=500)
    for x in rng.random(2000):
        detector.update(x)
    assert len(detector.window) <= 500