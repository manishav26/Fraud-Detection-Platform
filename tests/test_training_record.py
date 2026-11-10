"""Tests for the training record logged to MLflow (a tiny model, no real data)."""

import json

import lightgbm as lgb
import numpy as np
import pandas as pd
import pytest
from sklearn.isotonic import IsotonicRegression

from ml.model import FraudModel
from ml.train.training_record import git_commit, log_record, training_record


@pytest.fixture
def folder(tmp_path):
    rng = np.random.default_rng(0)
    X = pd.DataFrame({"x": rng.normal(size=300), "log_amount": rng.normal(4, 1, 300)})
    y = (X["x"] > 0.5).astype(int)
    m = lgb.LGBMClassifier(n_estimators=7, learning_rate=0.2, num_leaves=5, random_state=3,
                           verbose=-1).fit(X, y)
    cal = IsotonicRegression(out_of_bounds="clip").fit(m.predict_proba(X)[:, 1], y)
    info = {"name": "fraud-lgbm-v1", "train_days": "1-73", "calibration_days": "74-91",
            "test_days": "92-109", "n_features": 2, "lightgbm": lgb.__version__}
    FraudModel.from_training(m, {}, cal, {"review_threshold": 0.55,
                                          "decline_threshold": 0.7}, info).save(tmp_path)
    return tmp_path


class FakeClient:
    def __init__(self):
        self.params, self.metrics, self.tags = {}, {}, {}

    def log_param(self, run_id, key, value):
        self.params[key] = value

    def log_metric(self, run_id, key, value):
        self.metrics[key] = value

    def set_tag(self, run_id, key, value):
        self.tags[key] = value


def test_settings_come_from_the_model_file(folder):
    params = training_record(folder, with_metrics=False)["params"]
    assert params["n_estimators"] == 7
    assert params["learning_rate"] == pytest.approx(0.2)
    assert params["num_leaves"] == 5
    assert params["random_state"] == 3


def test_data_windows_thresholds_and_hashes_are_recorded(folder):
    record = training_record(folder, with_metrics=False)
    assert record["params"]["train_days"] == "1-73"
    assert record["params"]["test_days"] == "92-109"
    assert record["params"]["review_threshold"] == 0.55
    manifest = json.loads((folder / "manifest.json").read_text())
    assert record["tags"]["model_txt_sha256"] == manifest["model.txt"]
    assert record["tags"]["model_label"] == f"fraud-lgbm-v1-{manifest['model.txt'][:8]}"
    assert record["metrics"] == {}


def test_git_commit_is_recorded():
    commit, dirty = git_commit()
    assert commit == "unknown" or len(commit) == 40
    assert isinstance(dirty, bool)


def test_log_record_writes_everything_as_mlflow_values(folder):
    client = FakeClient()
    record = training_record(folder, with_metrics=False)
    record["metrics"] = {"test_pr_auc": 0.5}
    log_record(client, "run-1", record)
    assert client.params["num_leaves"] == "5"            # MLflow params are strings
    assert client.metrics == {"test_pr_auc": 0.5}
    assert client.tags["model_label"].startswith("fraud-lgbm-v1-")