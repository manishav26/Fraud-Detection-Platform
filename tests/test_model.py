"""Tests for the packaged model: save/load, integrity check, parity, cold start, decisions."""

import lightgbm as lgb
import numpy as np
import pandas as pd
import pytest
from sklearn.isotonic import IsotonicRegression

from features.history import HISTORY_FEATURES
from ml.model import FraudModel

THRESHOLDS = {"review_threshold": 0.39, "decline_threshold": 0.70}


@pytest.fixture(scope="module")
def trained():
    rng = np.random.default_rng(0)
    n = 3000
    X = pd.DataFrame({"C1": rng.integers(0, 5, n).astype(float),
                      "ProductCD": rng.choice(["W", "C", "H"], n),
                      "log_amount": rng.normal(4, 1, n),
                      "hour": rng.integers(0, 24, n)})
    for name in HISTORY_FEATURES:
        X[name] = rng.normal(0, 1, n)
    X.loc[::7, "C1"] = np.nan
    y = ((X["C1"] > 2) & (X["ProductCD"] == "C") | (rng.random(n) < 0.03)).astype(int)
    categories = {"ProductCD": pd.Index(sorted(X["ProductCD"].unique()))}
    X["ProductCD"] = pd.Categorical(X["ProductCD"], categories=categories["ProductCD"])
    lgbm = lgb.LGBMClassifier(n_estimators=30, num_leaves=8, verbose=-1).fit(X, y)
    raw = lgbm.predict_proba(X)[:, 1]
    calibrator = IsotonicRegression(out_of_bounds="clip").fit(raw, y)
    model = FraudModel.from_training(lgbm, categories, calibrator, THRESHOLDS, {"name": "test"})
    return model, lgbm, calibrator, X


def test_save_and_load_give_same_predictions(trained, tmp_path):
    model, _, _, X = trained
    model.save(tmp_path)
    loaded = FraudModel.load(tmp_path)
    assert np.allclose(model.predict_frame(X), loaded.predict_frame(X))


def test_packaged_model_matches_original(trained):
    model, lgbm, calibrator, X = trained
    expected = calibrator.predict(lgbm.predict_proba(X)[:, 1])
    assert np.allclose(model.predict_frame(X), expected, atol=1e-9)


def test_tampered_file_is_refused(trained, tmp_path):
    model, *_ = trained
    model.save(tmp_path)
    with open(tmp_path / "model.txt", "a") as f:
        f.write("\n# tampered")
    with pytest.raises(ValueError):
        FraudModel.load(tmp_path)


def test_predict_one_matches_batch(trained):
    model, *_ = trained
    history = {name: 0.5 for name in HISTORY_FEATURES}
    request = {"amount": float(np.expm1(4.2)), "ts": "2026-01-01T13:30:00Z",
               "attrs": {"C1": 3.0, "ProductCD": "C"}}
    row = {"C1": 3.0, "ProductCD": "C", "log_amount": 4.2, "hour": 13, **history}
    assert model.predict_one(request, history) == pytest.approx(model.predict_rows([row])[0])


def test_cold_start_and_unknown_category_still_score(trained):
    model, *_ = trained
    p = model.predict_one({"amount": 10.0, "ts": "2026-01-01T00:00:00Z",
                           "attrs": {"ProductCD": "NEVER_SEEN"}}, history=None)
    assert 0.0 <= p <= 1.0


def test_decisions_follow_thresholds(trained):
    model, *_ = trained
    assert model.decide(0.10) == "approve"
    assert model.decide(0.39) == "review"
    assert model.decide(0.69) == "review"
    assert model.decide(0.70) == "decline"