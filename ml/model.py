"""The packaged fraud model: what the scoring service loads.

A saved model is a folder with three files:
  model.txt       LightGBM model (its own text format, portable across versions)
  meta.json       feature order, category lists, calibration curve, thresholds
  manifest.json   SHA-256 of model.txt and meta.json, checked on load (threat 11)

Usage in the scoring service:
    model = FraudModel.load("models/fraud-lgbm-v1")
    prob = model.predict_one(request, history)        # calibrated fraud probability
    decision = model.decide(prob)                      # approve / review / decline
    reasons = model.explain_one(request, history)      # top 3 reasons (TreeSHAP)
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
MIN_WEIGHT = 0.01   # smaller pushes toward fraud are not worth showing as a reason


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _hour_from_ts(ts):
    """Hour of day (UTC) from an ISO timestamp string or datetime."""
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)      # Python 3.11+ understands the trailing "Z"
    if ts.tzinfo is not None:
        ts = ts.astimezone(UTC)
    return ts.hour


def _is_missing(value):
    return value is None or (isinstance(value, float) and math.isnan(value))


def _reason(name, value, weight):
    """One readable reason, e.g. 'amount=1250.00 (+0.84)'. weight = push toward fraud."""
    if _is_missing(value):
        shown = "missing"
    elif name == "log_amount":                     # show the real amount, not its log
        name, shown = "amount", f"{math.expm1(float(value)):.2f}"
    elif isinstance(value, (int, float, np.integer, np.floating)):
        shown = f"{float(value):g}"
    else:
        shown = str(value)
    return f"{name}={shown} (+{weight:.2f})"


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
        if _is_missing(value):
            return np.nan
        if name in self.categories:
            return float(self.categories[name].get(str(value), np.nan))   # unseen -> NaN
        return float(value)

    def _matrix(self, rows):
        return np.array([[self._encode(f, r.get(f)) for f in self.features] for r in rows],
                        dtype=float)

    def _row(self, request, history=None):
        """One API request (amount, ts, attrs) plus history features -> one feature dict.

        history=None means an unknown or new user (cold start): history features are missing.
        """
        row = dict(request.get("attrs") or {})
        row["log_amount"] = math.log1p(float(request["amount"]))
        row["hour"] = _hour_from_ts(request["ts"])
        history = history or {}
        for name in HISTORY_FEATURES:
            row[name] = history.get(name)
        return row

    def calibrate(self, raw):
        return np.interp(raw, self.cal_x, self.cal_y)

    def predict_rows(self, rows):
        """Calibrated probabilities for a list of dicts (feature name -> value)."""
        raw = self.booster.predict(self._matrix(rows), num_threads=1)
        return self.calibrate(raw)

    def predict_frame(self, df):
        """Calibrated probabilities for a pandas DataFrame with the feature columns."""
        return self.predict_rows(df[self.features].to_dict("records"))

    def predict_one(self, request, history=None):
        """Calibrated fraud probability for one API request (see _row)."""
        return float(self.predict_rows([self._row(request, history)])[0])

    # ---------- reasons (TreeSHAP) ----------

    def explain_rows(self, rows, top=3):
        """Top reasons per row: the features that pushed the score most toward fraud.

        LightGBM computes exact TreeSHAP values itself (pred_contrib=True): for each row, one
        number per feature plus a base value, and they add up to the model's raw score. Only
        features that raised the risk (by at least MIN_WEIGHT) are reported, largest first, so
        a row can have fewer than `top` reasons.
        """
        contrib = self.booster.predict(self._matrix(rows), pred_contrib=True, num_threads=1)
        reasons = []
        for row, c in zip(rows, contrib):
            c = c[:-1]                              # last column is the base value
            order = np.argsort(-c)[:top]
            reasons.append([_reason(self.features[i], row.get(self.features[i]), c[i])
                            for i in order if c[i] >= MIN_WEIGHT])
        return reasons

    def explain_one(self, request, history=None, top=3):
        """Top reasons for one API request, same inputs as predict_one."""
        return self.explain_rows([self._row(request, history)], top)[0]

    def decide(self, prob):
        t = self.meta["thresholds"]
        if prob >= t["decline_threshold"]:
            return "decline"
        return "review" if prob >= t["review_threshold"] else "approve"