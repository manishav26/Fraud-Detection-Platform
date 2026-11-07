"""Tests for the E6 evasion experiment (made-up models and payments, no real data)."""

import math

import lightgbm as lgb
import numpy as np
import pandas as pd
import pytest
from sklearn.isotonic import IsotonicRegression

from ml.experiments.e6_evasion import (
    attack,
    encode,
    mutate,
    over_budget,
    probe_frozen,
    success,
    value_pools,
)
from ml.model import FraudModel


class RuleModel:
    """Fraud if it's night and the e-mail is anonymous; the score drops as each is fixed."""

    features = ("log_amount", "hour", "P_emaildomain")

    def predict_rows(self, rows):
        return np.array([0.3 * (r["hour"] < 6) + 0.5 * (r["P_emaildomain"] == "anon.com")
                         + 0.1 for r in rows])

    def decide(self, p):
        return "decline" if p >= 0.7 else "review" if p >= 0.35 else "approve"


ORIGINAL = {"log_amount": math.log1p(400.0), "hour": 2, "P_emaildomain": "anon.com"}
POOLS = {"P_emaildomain": np.array(["gmail.com", "anon.com", None], dtype=object)}


def test_mutate_changes_one_field_and_keeps_most_of_the_money():
    rng = np.random.default_rng(0)
    for _ in range(500):
        new = mutate(ORIGINAL, ORIGINAL, rng, RuleModel.features, POOLS)
        changed = [f for f in ORIGINAL if new[f] != ORIGINAL[f]]
        assert len(changed) <= 1
        assert 200 - 1e-6 <= math.expm1(new["log_amount"]) <= 400 + 1e-6
        assert 0 <= new["hour"] < 24


def test_both_attackers_eventually_get_the_payment_approved():
    rng = np.random.default_rng(0)
    for feedback in ["score", "decision"]:
        first = attack(RuleModel(), [ORIGINAL] * 50, rng, POOLS, 200, feedback)
        assert np.isfinite(first).mean() > 0.9


def test_seeing_the_score_helps_the_attacker():
    def mean_queries(feedback):
        first = attack(RuleModel(), [ORIGINAL] * 300, np.random.default_rng(1), POOLS, 200,
                       feedback)
        return np.mean(first[np.isfinite(first)])
    assert mean_queries("score") <= mean_queries("decision")


def test_success_applies_budget_and_probe_detection():
    first = np.array([3, 8, 15, 30, 120, np.inf])
    assert success(first, 200, "no defense") == pytest.approx(5 / 6)
    assert success(first, 10, "D1") == pytest.approx(2 / 6)
    assert success(first, 200, "D1+D2", budget=20) == pytest.approx(3 / 6)
    assert success(first, 200, "D1+D2+D3", budget=20, probe_flags=5) == pytest.approx(1 / 6)
    assert success(first, 200, "D1+D3", budget=20, probe_flags=5) == pytest.approx(1 / 6)


def test_over_budget_counts_per_card_per_day():
    uids = ["a"] * 4 + ["b"] * 2 + ["a"] * 2
    days = [1, 1, 1, 1, 1, 1, 2, 2]
    assert over_budget(uids, days, budget=3).tolist() == [False, False, False, True,
                                                          False, False, False, False]


def test_probe_detection_freezes_a_card_only_within_the_window():
    uids = ["a"] * 5 + ["b"] * 3
    times = np.array([0, 10, 20, 30, 40, 0, 10, 20])
    flagged = np.array([1, 1, 1, 0, 0, 1, 1, 1], dtype=bool)
    frozen = probe_frozen(uids, times, flagged, probe_flags=3, window=100)
    assert frozen.tolist() == [False, False, False, True, True, False, False, False]
    later = probe_frozen(["a"] * 4, np.array([0, 10, 20, 500]), np.ones(4, bool),
                         probe_flags=3, window=100)
    assert later.tolist() == [False, False, False, False]


def test_vectorised_encoding_matches_the_model():
    rng = np.random.default_rng(0)
    n = 400
    df = pd.DataFrame({"x": rng.normal(size=n),
                       "c": rng.choice(["a", "b", None], n),
                       "log_amount": rng.normal(4, 1, n)})
    y = (df["x"] > 0.5).astype(int)
    X = df.copy()
    X["c"] = X["c"].astype("category")
    m = lgb.LGBMClassifier(n_estimators=10, verbose=-1).fit(X, y)
    cal = IsotonicRegression(out_of_bounds="clip").fit(m.predict_proba(X)[:, 1], y)
    model = FraudModel.from_training(m, {"c": X["c"].cat.categories}, cal,
                                     {"review_threshold": 0.5, "decline_threshold": 0.7}, {})
    expected = model._matrix(df[model.features].to_dict("records"))
    assert np.allclose(encode(model, df), expected, equal_nan=True)


def test_value_pools_keep_what_customers_really_send():
    pools = value_pools([{"P_emaildomain": "gmail.com"}, {"P_emaildomain": None}],
                        ["P_emaildomain"])
    assert list(pools["P_emaildomain"]) == ["gmail.com", None]