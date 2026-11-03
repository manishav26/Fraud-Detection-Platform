"""Steps 5-6: calibrate LightGBM scores and propose decision thresholds.

1. Train LightGBM on days 1-73 (the 65 features in features/schema.yaml, raw D).
2. Split validation by time: days 74-91 fit the calibrator, days 92-109 judge it.
3. Isotonic calibration: map raw scores to probabilities that match observed fraud rates.
4. Choose the review threshold that minimises expected cost on days 74-91:
   cost = amount of fraud that gets approved + review_cost for every flagged payment.
5. Report everything on days 92-109, which neither the model nor the calibrator saw.
"""

from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
import yaml
from sklearn.calibration import calibration_curve
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import average_precision_score, brier_score_loss

from features.history import HISTORY_FEATURES, add_history
from features.proxy_id import add_proxy_id
from features.request_time import hour_of_day, log_amount

ROOT = Path(__file__).resolve().parents[2]
PROCESSED = ROOT / "data" / "processed"
RESULTS = ROOT / "docs" / "results"
FIGURES = ROOT / "docs" / "figures"

NUMERIC = (["card1", "card2", "card3", "card5", "addr1", "addr2", "dist1"]
           + [f"C{i}" for i in range(1, 15)] + [f"D{i}" for i in range(1, 16)])
CATEGORICAL = (["ProductCD", "card4", "card6", "P_emaildomain", "R_emaildomain", "DeviceType"]
               + [f"M{i}" for i in range(1, 10)])
FEATURES = NUMERIC + ["log_amount", "hour"] + CATEGORICAL + HISTORY_FEATURES
TARGET = "isFraud"
DECLINE_THRESHOLD = 0.70          # calibrated: decline when fraud is more likely than not, by a margin
REVIEW_COSTS = [2.0, 5.0, 10.0]   # assumed cost of one analyst review, in dollars
MAIN_REVIEW_COST = 5.0
REVIEW_CAPACITY = 0.02            # analysts can review at most 2% of payments

BASE_PARAMS = {"n_estimators": 400, "learning_rate": 0.05, "num_leaves": 64}  # untuned v1
PARAMS_FILE = ROOT / "ml" / "configs" / "lgbm.yaml"


def model_params():
    """Tuned settings from ml/configs/lgbm.yaml (written by tune.py), else the v1 defaults."""
    if PARAMS_FILE.exists():
        return yaml.safe_load(PARAMS_FILE.read_text())
    return dict(BASE_PARAMS)
def load_data():
    cols = NUMERIC + CATEGORICAL + ["TransactionDT", "TransactionAmt", TARGET]
    parts = []
    for split in ["train", "val", "stream"]:
        part = pd.read_parquet(PROCESSED / f"{split}.parquet", columns=cols)
        part["split"] = split
        parts.append(part)
    df = pd.concat(parts, ignore_index=True)
    df["log_amount"] = log_amount(df["TransactionAmt"])
    df["hour"] = hour_of_day(df["TransactionDT"])
    return add_history(add_proxy_id(df))


def to_model_input(df, categories=None):
    X = df[FEATURES].copy()
    categories = categories or {c: X[c].astype("category").cat.categories for c in CATEGORICAL}
    for c in CATEGORICAL:
        X[c] = pd.Categorical(X[c], categories=categories[c])
    return X, categories


def train_model(train):
    X, categories = to_model_input(train)
    model = lgb.LGBMClassifier(**model_params(), random_state=42, n_jobs=8,
                               deterministic=True, force_col_wise=True, verbose=-1)
    model.fit(X, train[TARGET])
    return model, categories


def fit_calibrator(raw_scores, y):
    """Isotonic regression: a step function from raw score to observed fraud rate."""
    return IsotonicRegression(y_min=0, y_max=1, out_of_bounds="clip").fit(raw_scores, y)


def expected_cost(probs, y, amount, review_threshold, review_cost):
    """Missed fraud dollars + review cost for every flagged payment (review or decline)."""
    flagged = probs >= review_threshold
    missed = (y == 1) & ~flagged
    return float(amount[missed].sum() + review_cost * flagged.sum())


def best_review_threshold(probs, y, amount, review_cost, capacity=REVIEW_CAPACITY):
    """Cheapest threshold whose flagged share stays within analyst capacity."""
    grid = np.round(np.arange(0.01, DECLINE_THRESHOLD, 0.01), 2)
    allowed = [t for t in grid if (probs >= t).mean() <= capacity]
    if not allowed:              # capacity too small: only declines get flagged
        return DECLINE_THRESHOLD
    costs = [expected_cost(probs, y, amount, t, review_cost) for t in allowed]
    return float(allowed[int(np.argmin(costs))])


def decision_counts(probs, y, review_t, decline_t):
    decision = np.where(probs >= decline_t, "decline",
                        np.where(probs >= review_t, "review", "approve"))
    table = pd.crosstab(pd.Series(decision, name="decision"),
                        pd.Series(np.where(y == 1, "fraud", "normal"), name="actually"))
    return table.reindex(["approve", "review", "decline"]).fillna(0).astype(int)


def save_reliability_plot(y, raw, calibrated, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(5, 5))
    ax.plot([0, 1], [0, 1], linestyle="--", color="grey", label="perfect")
    for name, scores in [("raw LightGBM", raw), ("calibrated", calibrated)]:
        frac, mean = calibration_curve(y, scores, n_bins=10, strategy="quantile")
        ax.plot(mean, frac, marker="o", label=name)
    ax.set_xlabel("predicted fraud probability")
    ax.set_ylabel("observed fraud rate")
    ax.set_title("Reliability (days 92-109)")
    ax.legend()
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)


def main():
    print("loading data and building features...")
    df = load_data()
    train = df[df["split"] == "train"]
    val = df[df["split"] == "val"]
    mid_day = val["TransactionDT"].min() // 86400 + 18          # days 74-91 | 92-109
    cal = val[val["TransactionDT"] // 86400 < mid_day]
    test = val[val["TransactionDT"] // 86400 >= mid_day]
    print(f"train {len(train)}, calibration {len(cal)}, test {len(test)}")

    print("training LightGBM...")
    model, categories = train_model(train)
    raw_cal = model.predict_proba(to_model_input(cal, categories)[0])[:, 1]
    raw_test = model.predict_proba(to_model_input(test, categories)[0])[:, 1]

    calibrator = fit_calibrator(raw_cal, cal[TARGET])
    cal_probs = calibrator.predict(raw_cal)
    test_probs = calibrator.predict(raw_test)
    y_test, y_cal = test[TARGET].to_numpy(), cal[TARGET].to_numpy()

    quality = pd.DataFrame([
        {"scores": "raw LightGBM", "Brier score": brier_score_loss(y_test, raw_test),
         "mean predicted": raw_test.mean(), "PR-AUC": average_precision_score(y_test, raw_test)},
        {"scores": "calibrated", "Brier score": brier_score_loss(y_test, test_probs),
         "mean predicted": test_probs.mean(),
         "PR-AUC": average_precision_score(y_test, test_probs)},
    ]).round(4)
    print(f"\nactual fraud rate (days 92-109): {y_test.mean():.4f}")
    print(quality.to_string(index=False))

    rows = []
    for cost in REVIEW_COSTS:
        t = best_review_threshold(cal_probs, y_cal, cal["TransactionAmt"].to_numpy(), cost)
        flagged = test_probs >= t
        rows.append({"review cost ($)": cost, "review threshold": t,
                     "flagged (test)": int(flagged.sum()),
                     "share flagged": f"{flagged.mean():.1%}",
                     "frauds caught (test)": int((flagged & (y_test == 1)).sum()),
                     "frauds total (test)": int(y_test.sum()),
                     "expected cost (test, $)": round(expected_cost(
                         test_probs, y_test, test["TransactionAmt"].to_numpy(), t, cost))})
    thresholds = pd.DataFrame(rows)
    print("\n", thresholds.to_string(index=False))

    review_t = float(thresholds.loc[thresholds["review cost ($)"] == MAIN_REVIEW_COST,
                                    "review threshold"].iloc[0])
    counts = decision_counts(test_probs, y_test, review_t, DECLINE_THRESHOLD)
    print(f"\nproposed: review >= {review_t}, decline >= {DECLINE_THRESHOLD} "
          f"(review cost ${MAIN_REVIEW_COST:.0f})\n")
    print(counts)

    save_reliability_plot(y_test, raw_test, test_probs, FIGURES / "reliability.png")
    RESULTS.mkdir(parents=True, exist_ok=True)
    with open(RESULTS / "calibration.md", "w") as f:
        f.write("# Calibration and decision thresholds (week 2, steps 5-6)\n\n")
        f.write("LightGBM trained on days 1-73 (65 features, raw D). Isotonic calibration "
                "fitted on days 74-91; everything below is measured on days 92-109.\n\n")
        f.write(f"Actual fraud rate on days 92-109: {y_test.mean():.4f}\n\n")
        f.write("## Calibration quality\n\n" + quality.to_markdown(index=False) + "\n\n")
        f.write("![reliability](../figures/reliability.png)\n\n")
        f.write("## Review threshold by assumed review cost\n\n"
                "Chosen on days 74-91 to minimise missed fraud dollars + review cost per "
                "flagged payment; evaluated on days 92-109.\n\n")
        f.write(thresholds.to_markdown(index=False) + "\n\n")
        f.write(f"## Proposed decision rule (review cost ${MAIN_REVIEW_COST:.0f})\n\n")
        f.write(f"approve below {review_t}, review from {review_t} to {DECLINE_THRESHOLD}, "
                f"decline at {DECLINE_THRESHOLD} or above (calibrated probabilities).\n\n")
        f.write(counts.to_markdown() + "\n")
    print("\nsaved docs/results/calibration.md and docs/figures/reliability.png")
    print("\nproposed config/decision.yaml values:")
    print(f"review_threshold: {review_t}\ndecline_threshold: {DECLINE_THRESHOLD}")


if __name__ == "__main__":
    main()