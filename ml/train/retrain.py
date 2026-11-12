"""Retrain on recent stream days and package the result like model v1, ready to register.

This is the retrain E3's strategies do on day 168, as a real model folder: on day 168 the labels
of days up to 154 are known (14 days late), so the model trains on days 95-140, is calibrated
on 141-154, and is held out on 169-182, the days after it would go live. Same features, tuned
settings, isotonic calibration and 2% capacity rule as v1; only the days differ.

    python -m ml.train.retrain                       # ~1-2 min -> models/fraud-lgbm-v2/
    python -m ml.eval.promotion_gate --candidate models/fraud-lgbm-v2 --current models/fraud-lgbm-v1

The gate judges both models on v2's test days (169-182), which neither has seen. Writes
docs/results/retrain_v2.md. Does not touch models/fraud-lgbm-v1 (the pinned model).
"""

import argparse
from pathlib import Path

import lightgbm
import numpy as np
from sklearn.metrics import average_precision_score, roc_curve

from ml.eval.promotion_gate import golden_set, parse_days
from ml.labels.label_sim import DEFAULT_DELAY_DAYS
from ml.model import FraudModel
from ml.train.calibrate import (
    DECLINE_THRESHOLD,
    FEATURES,
    RESULTS,
    TARGET,
    capacity_review_threshold,
    fit_calibrator,
    load_data,
    model_params,
    to_model_input,
    train_model,
)

ROOT = Path(__file__).resolve().parents[2]
NAME = "fraud-lgbm-v2"
TRAIN_DAYS, CALIBRATION_DAYS, TEST_DAYS = "95-140", "141-154", "169-182"


def check_windows(train, calibration, test, delay=DEFAULT_DELAY_DAYS):
    """Windows must come in order, and the test must start after the labels of the training
    and calibration days could be known (last labelled day + label delay)."""
    (t0, t1), (c0, c1), (s0, s1) = (parse_days(d) for d in (train, calibration, test))
    if not (t1 < c0 and c1 < s0):
        raise ValueError("train, calibration and test days must not overlap and must be in order")
    if s0 <= c1 + delay:
        raise ValueError(f"test starts on day {s0}, but the labels of day {c1} are only known on "
                         f"day {c1 + delay}: start the test after that")
    return (t0, t1), (c0, c1), (s0, s1)


def recall_at_fpr(y, s, fpr_target=0.01):
    fpr, tpr, _ = roc_curve(y, s)
    return float(np.interp(fpr_target, fpr, tpr))


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--train-days", default=TRAIN_DAYS)
    p.add_argument("--calibration-days", default=CALIBRATION_DAYS)
    p.add_argument("--test-days", default=TEST_DAYS)
    p.add_argument("--out", default=ROOT / "models" / NAME)
    args = p.parse_args()
    train_days, cal_days, test_days = check_windows(args.train_days, args.calibration_days,
                                                    args.test_days)
    out = Path(args.out)
    if out.resolve() == (ROOT / "models" / "fraud-lgbm-v1").resolve():
        raise SystemExit("refusing to overwrite the pinned model folder models/fraud-lgbm-v1")

    print("loading data and building features...")
    df = load_data()
    train, cal, test = (golden_set(df, d) for d in (train_days, cal_days, test_days))
    print(f"train {len(train):,}, calibration {len(cal):,}, test {len(test):,} payments")

    print("training LightGBM (tuned v1 settings)...")
    lgbm, categories = train_model(train)
    calibrator = fit_calibrator(
        lgbm.predict_proba(to_model_input(cal, categories)[0])[:, 1], cal[TARGET])
    cal_probs = calibrator.predict(lgbm.predict_proba(to_model_input(cal, categories)[0])[:, 1])
    thresholds = {"review_threshold": capacity_review_threshold(cal_probs),
                  "decline_threshold": float(DECLINE_THRESHOLD)}

    info = {"name": NAME, "train_days": args.train_days, "calibration_days": args.calibration_days,
            "test_days": args.test_days, "n_features": len(FEATURES),
            "lightgbm": lightgbm.__version__}
    model = FraudModel.from_training(lgbm, categories, calibrator, thresholds, info)
    manifest = model.save(out)

    loaded = FraudModel.load(out)
    probs = loaded.predict_frame(test)
    y = test[TARGET].to_numpy()
    pr_auc, recall = average_precision_score(y, probs), recall_at_fpr(y, probs)
    flagged = float((probs >= thresholds["review_threshold"]).mean())
    print(f"saved {out}")
    print(f"test days {args.test_days}: PR-AUC {pr_auc:.4f}, recall @ 1% FPR {recall:.4f}, "
          f"flagged {flagged:.2%}, review threshold {thresholds['review_threshold']}")
    for f, h in manifest.items():
        print(f"  {f}  {h}")

    RESULTS.mkdir(parents=True, exist_ok=True)
    settings = ", ".join(f"{k} {v}" for k, v in model_params().items())
    (RESULTS / "retrain_v2.md").write_text(
        f"# Retrained model {NAME}\n\n"
        f"The retrain E3 does on day 168, packaged like v1. Labels arrive "
        f"{DEFAULT_DELAY_DAYS} days late, so on day 168 days up to 154 are known.\n\n"
        f"| Part | Days | Payments |\n|---|---|---|\n"
        f"| Train | {args.train_days} | {len(train):,} |\n"
        f"| Calibration | {args.calibration_days} | {len(cal):,} |\n"
        f"| Test (held out) | {args.test_days} | {len(test):,} |\n\n"
        f"Settings: {settings} (tuned v1, ml/configs/lgbm.yaml). Review threshold "
        f"{thresholds['review_threshold']} (lowest within 2% on the calibration days), decline "
        f"{DECLINE_THRESHOLD}.\n\n"
        f"Test days {args.test_days}: PR-AUC {pr_auc:.4f}, recall @ 1% FPR {recall:.4f}, "
        f"flagged {flagged:.2%}.\n\n"
        "| File | SHA-256 |\n|---|---|\n"
        + "".join(f"| {f} | `{h}` |\n" for f, h in manifest.items())
        + f"\nCompare with the live model: `python -m ml.eval.promotion_gate --candidate {out} "
          "--current models/fraud-lgbm-v1` (judged on the test days above).\n")
    print("saved docs/results/retrain_v2.md")


if __name__ == "__main__":
    main()