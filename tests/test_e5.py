"""Tests for the E5 drift-injection experiment (small made-up streams, no real data)."""

import numpy as np
import pandas as pd
import pytest

from ml.drift.detectors import psi, psi_categorical
from ml.experiments.e5_drift_detectors import (
    ReferencePSI,
    calm_stream,
    inject,
    outcome,
)
from ml.train.calibrate import FEATURES, TARGET


def make_stream(days=range(110, 130), per_day=50, seed=0):
    rng = np.random.default_rng(seed)
    n = len(days) * per_day
    df = pd.DataFrame({f: rng.normal(0, 1, n) for f in FEATURES})
    df["log_amount"] = np.log1p(rng.uniform(1, 500, n))
    df["TransactionAmt"] = np.expm1(df["log_amount"])
    for c in ["M4", "D1"]:
        df[c] = 1.0
    df["day"] = np.repeat(list(days), per_day)
    df[TARGET] = (rng.random(n) < 0.1).astype(int)
    return df


def test_calm_stream_keeps_every_payment_and_the_daily_counts():
    s = make_stream()
    calm = calm_stream(s, np.random.default_rng(1))
    assert len(calm) == len(s)
    assert sorted(calm[TARGET]) == sorted(s[TARGET])
    assert (calm["day"].value_counts().sort_index() == s["day"].value_counts().sort_index()).all()


def test_amounts_x3_changes_only_days_from_the_start():
    s = make_stream()
    out = inject(s, "amounts x3", 120, np.random.default_rng(0))
    before, after = s["day"] < 120, s["day"] >= 120
    assert np.allclose(out.loc[before, "log_amount"], s.loc[before, "log_amount"])
    assert np.allclose(np.expm1(out.loc[after, "log_amount"]),
                       3 * np.expm1(s.loc[after, "log_amount"]))


def test_missing_fields_grows_gradually():
    s = make_stream(days=range(110, 160), per_day=200)
    out = inject(s, "missing fields", 120, np.random.default_rng(0))
    share = out.groupby("day")["M4"].apply(lambda x: x.isna().mean())
    assert share.loc[:119].max() == 0
    assert share.loc[120] < 0.15 < share.loc[130] < share.loc[145]
    assert share.loc[145] == pytest.approx(0.8, abs=0.1)


def test_fraud_looks_normal_keeps_labels_and_copies_normal_rows():
    s = make_stream(per_day=200)
    out = inject(s, "fraud looks normal", 120, np.random.default_rng(0))
    assert (out[TARGET] == s[TARGET]).all()
    before = s["day"] < 120
    assert out.loc[before, FEATURES].equals(s.loc[before, FEATURES])
    frauds = s.index[(s["day"] >= 120) & (s[TARGET] == 1)]
    changed = (out.loc[frauds, FEATURES] != s.loc[frauds, FEATURES]).any(axis=1)
    assert 0.3 < changed.mean() < 0.7
    normals = s[s[TARGET] == 0]
    copied = out.loc[frauds[changed.to_numpy()], "C1"] if "C1" in FEATURES else []
    assert set(np.round(copied, 9)) <= set(np.round(normals["C1"], 9))


def test_unknown_scenario_is_an_error():
    with pytest.raises(ValueError):
        inject(make_stream(), "aliens", 120, np.random.default_rng(0))


def test_reference_psi_matches_the_detectors_module():
    rng = np.random.default_rng(0)
    a, b = rng.normal(0, 1, 5000), rng.normal(0.5, 1, 2000)
    assert ReferencePSI(a)(b) == pytest.approx(psi(a, b))
    x = rng.choice(["a", "b", None], 3000)
    y = rng.choice(["a", "b", "c"], 2000)
    assert ReferencePSI(x, categorical=True)(y) == pytest.approx(psi_categorical(x, y))


def test_outcome_counts_delay_and_false_alarms():
    assert outcome([125, 141, 150], 140) == {"delay": 1, "false alarms": 1}
    assert outcome([125], 140) == {"delay": None, "false alarms": 1}