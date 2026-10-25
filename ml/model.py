"""The packaged fraud model: what the scoring service loads.

A saved model is a folder with three files:
  model.txt       LightGBM model (its own text format, portable across versions)
  meta.json       feature order, category lists, calibration curve, thresholds
  manifest.json   SHA-256 of model.txt and meta.json, checked on load (threat 11)

Usage in the scoring service:
    model = FraudModel.load("models/fraud-lgbm-v1")
    prob = model.predict_one(request, history)        # calibrated fraud probability
    decision = model.decide(prob)                      # approve / review / decline
"""

import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path

import lightgbm as lgb
import numpy as np

from features.history import HISTORY_FEATURES

FILES = ["model.txt", "meta.json"]


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _hour_from_ts(ts):
    """Hour of day (UTC) from an ISO timestamp string or datetime."""
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)      # Python 3.11+ understands the trailing "Z"
    if ts.tzinfo is not None:
        ts = ts.astimezone(UTC)
    return ts.hour

class FraudModel:
    def __init__(self, booster, meta):
        self.booster = booster
        self.meta = meta
        self.features = meta["features"]
        self.categories = {c: {v: i for i, v in enumerate(vals)}
                           for c, vals in meta["categories"].items()}
        self.cal_x = np.asarray(meta["calibration"]["x"], dtype=float)
        self.cal_y = np.asarray(meta["calibration"]["y"], dtype=float)

    # ---------- building from a trained LightGBM model ----------

    @classmethod
    def from_training(cls, lgbm_model, categories, calibrator, thresholds, info):
        meta = {
            "features": list(lgbm_model.feature_name_),
            "categories": {c: [str(v) for v in vals] for c, vals in categories.items()},
            "calibration": {"x": calibrator.X_thresholds_.tolist(),
                            "y": calibrator.y_thresholds_.tolist()},
            "thresholds": thresholds,
            "info": info,
        }
        return cls(lgbm_model.booster_, meta)

    # ---------- save / load ----------

    def save(self, folder):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        self.booster.save_model(str(folder / "model.txt"))
        (folder / "meta.json").write_text(json.dumps(self.meta, indent=2))
        manifest = {f: _sha256(folder / f) for f in FILES}
        (folder / "manifest.json").write_text(json.dumps(manifest, indent=2))
        return manifest

    @classmethod
    def load(cls, folder):
        folder = Path(folder)
        manifest = json.loads((folder / "manifest.json").read_text())
        for f in FILES:
            if _sha256(folder / f) != manifest[f]:
                raise ValueError(f"{f} does not match its SHA-256 in manifest.json; refusing to load")
        booster = lgb.Booster(model_file=str(folder / "model.txt"))
        return cls(booster, json.loads((folder / "meta.json").read_text()))

    # ---------- prediction ----------

    def _encode(self, name, value):
        """Numbers stay numbers; categories become their training index; missing -> NaN."""
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return np.nan
        if name in self.categories:
            return float(self.categories[name].get(str(value), np.nan))   # unseen -> NaN
        return float(value)

    def calibrate(self, raw):
        return np.interp(raw, self.cal_x, self.cal_y)

    def predict_rows(self, rows):
        """Calibrated probabilities for a list of dicts (feature name -> value)."""
        X = np.array([[self._encode(f, r.get(f)) for f in self.features] for r in rows],
                     dtype=float)
        raw = self.booster.predict(X, num_threads=1)
        return self.calibrate(raw)

    def predict_frame(self, df):
        """Calibrated probabilities for a pandas DataFrame with the feature columns."""
        return self.predict_rows(df[self.features].to_dict("records"))

    def predict_one(self, request, history=None):
        """One API request (amount, ts, attrs) plus history features from Cassandra.

        history=None means an unknown or new user (cold start): history features are missing.
        """
        row = dict(request.get("attrs") or {})
        row["log_amount"] = math.log1p(float(request["amount"]))
        row["hour"] = _hour_from_ts(request["ts"])
        history = history or {}
        for name in HISTORY_FEATURES:
            row[name] = history.get(name)
        return float(self.predict_rows([row])[0])

    def decide(self, prob):
        t = self.meta["thresholds"]
        if prob >= t["decline_threshold"]:
            return "decline"
        return "review" if prob >= t["review_threshold"] else "approve"