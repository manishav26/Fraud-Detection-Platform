"""Model v1 tuning: random search over LightGBM settings (week 3, step 3).

Trains on days 1-73 and scores each trial on days 74-91 (the calibration window). Days 92-109
stay untouched, so the model card numbers remain a fair test. Trial 0 is the current setting.
The best trial is adopted only if it beats the current setting by more than seed noise
(E1: about 0.002 PR-AUC), so we never switch for luck.

    python -m ml.train.tune            # 25 trials, about 10-20 minutes
Writes docs/results/tuning.md and ml/configs/lgbm.yaml (read by train_model).
"""

import time

import lightgbm as lgb
import numpy as np
import pandas as pd
import yaml
from sklearn.metrics import average_precision_score, roc_curve

from ml.train.calibrate import (
    BASE_PARAMS,
    PARAMS_FILE,
    RESULTS,
    TARGET,
    load_data,
    to_model_input,
)

N_TRIALS = 25
MIN_GAIN = 0.005      # more than 2x the seed spread seen in E1
MAX_SIZE = 40_000     # trees x leaves; trial 12 (1200 x 256) made TreeSHAP reasons 68 ms (budget 10)
THREADS = 8


def sample_params(rng):
    return {
        "n_estimators": int(rng.choice([200, 400, 800, 1200])),
        "learning_rate": round(float(np.exp(rng.uniform(np.log(0.01), np.log(0.1)))), 4),
        "num_leaves": int(rng.choice([16, 31, 64, 128, 256])),
        "min_child_samples": int(rng.choice([10, 20, 50, 100, 200])),
        "subsample": round(float(rng.uniform(0.6, 1.0)), 2),
        "subsample_freq": 1,
        "colsample_bytree": round(float(rng.uniform(0.5, 1.0)), 2),
        "reg_lambda": round(float(np.exp(rng.uniform(np.log(1e-3), np.log(10)))), 4),
    }


def recall_at_fpr(y, s, fpr_target=0.01):
    fpr, tpr, _ = roc_curve(y, s)
    return float(np.interp(fpr_target, fpr, tpr))


def main():
    print("loading data and building features...")
    df = load_data()
    train = df[df["split"] == "train"]
    val = df[df["split"] == "val"]
    mid_day = val["TransactionDT"].min() // 86400 + 18
    tune = val[val["TransactionDT"] // 86400 < mid_day]          # days 74-91 only
    X_train, categories = to_model_input(train)
    X_tune, _ = to_model_input(tune, categories)
    y_train, y_tune = train[TARGET], tune[TARGET]
    print(f"train {len(train):,}, tuning window {len(tune):,} (days 74-91)")

    rng = np.random.default_rng(0)
    trials = [dict(BASE_PARAMS)] + [sample_params(rng) for _ in range(N_TRIALS - 1)]
    rows = []
    for i, params in enumerate(trials):
        started = time.time()
        model = lgb.LGBMClassifier(**params, random_state=42, n_jobs=THREADS,
                                   deterministic=True, force_col_wise=True, verbose=-1)
        model.fit(X_train, y_train)
        scores = model.predict_proba(X_tune)[:, 1]
        rows.append({"trial": i, **params,
                     "PR-AUC": average_precision_score(y_tune, scores),
                     "recall @ 1% FPR": recall_at_fpr(y_tune, scores),
                     "minutes": (time.time() - started) / 60})
        r = rows[-1]
        print(f"  trial {i:2d}{' (current v1)' if i == 0 else ''}: PR-AUC {r['PR-AUC']:.4f}, "
              f"recall@1%FPR {r['recall @ 1% FPR']:.4f} ({r['minutes']:.1f} min)")

    results = pd.DataFrame(rows).sort_values("PR-AUC", ascending=False, ignore_index=True)
    results["trees x leaves"] = results["n_estimators"] * results["num_leaves"]
    baseline = results.loc[results["trial"] == 0, "PR-AUC"].iloc[0]
    best = results[results["trees x leaves"] <= MAX_SIZE].iloc[0]   # reasons must fit 10 ms
    gain = best["PR-AUC"] - baseline
    adopt = gain > MIN_GAIN
    chosen = trials[int(best["trial"])] if adopt else dict(BASE_PARAMS)
    PARAMS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PARAMS_FILE.write_text("# LightGBM settings for model v1, chosen by ml/train/tune.py\n"
                           + yaml.safe_dump(chosen, sort_keys=False))

    verdict = (f"Adopted trial {int(best['trial'])}: +{gain:.4f} PR-AUC over the current "
               "setting, more than the seed noise." if adopt else
               f"Kept the current setting: the best trial gained only {gain:+.4f} PR-AUC "
               f"(threshold {MIN_GAIN}), which is within seed noise.")
    print(f"\n{verdict}\nsaved {PARAMS_FILE.relative_to(PARAMS_FILE.parents[2])}")

    show = results.drop(columns=["subsample_freq"]).round(4)
    with open(RESULTS / "tuning.md", "w") as f:
        f.write("# Model v1 tuning (random search)\n\n")
        f.write(f"{N_TRIALS} LightGBM settings, trained on days 1-73, scored on days 74-91 "
                f"({len(tune):,} payments, {int(y_tune.sum()):,} frauds). Days 92-109 were "
                "not used. Trial 0 is the untuned v1 setting. Seed 42, fixed threads, "
                "deterministic mode.\n\n")
        f.write(f"## Decision\n\n{verdict}\n\nChosen settings (ml/configs/lgbm.yaml):\n\n")
        f.write("```yaml\n" + yaml.safe_dump(chosen, sort_keys=False) + "```\n\n")
        f.write("## All trials (best first)\n\n")
        f.write(show.to_markdown(index=False) + "\n")
    print("saved docs/results/tuning.md")


if __name__ == "__main__":
    main()