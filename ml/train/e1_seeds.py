"""E1 with 3 seeds: LightGBM vs XGBoost vs logistic regression, raw vs normalized D.

Same data as the v1 model: the 65 features (request-time + customer history), trained on
days 1-73, scored on validation days 74-109. Each tree model runs with 3 seeds; both use
row and column sampling (0.8), so the seed actually changes the model and the spread shows
how much of a difference is just luck. Logistic regression has no randomness: it runs once.

    python -m ml.train.e1_seeds          # about 20-30 minutes on a laptop
"""

import time

import lightgbm as lgb
import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_curve
from sklearn.pipeline import Pipeline, make_pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from ml.train.calibrate import CATEGORICAL, FEATURES, RESULTS, TARGET, load_data

SEEDS = [1, 2, 3]
THREADS = 8   # fixed, so a rerun on the same machine gives the same numbers
# D8 and D9 are not plain day counts, so they stay raw (same as e2_dnorm.py)
D_TO_NORMALIZE = [f"D{i}" for i in range(1, 16) if i not in (8, 9)]


def add_normalized_d(df):
    """D_norm = transaction day - D: the date the counted event happened."""
    day = df["TransactionDT"] / 86400
    for col in D_TO_NORMALIZE:
        df[f"{col}_norm"] = np.floor(day - df[col])
    return df


def recall_at_fpr(y, s, fpr_target=0.01):
    fpr, tpr, _ = roc_curve(y, s)
    return float(np.interp(fpr_target, fpr, tpr))


def tree_inputs(train, val, features):
    """Categorical columns as pandas categories, learned from train only."""
    X_train, X_val = train[features].copy(), val[features].copy()
    for c in CATEGORICAL:
        X_train[c] = X_train[c].astype("category")
        X_val[c] = pd.Categorical(X_val[c], categories=X_train[c].cat.categories)
    return X_train, X_val


def run_lightgbm(X_train, y_train, X_val, seed):
    model = lgb.LGBMClassifier(
        n_estimators=400, learning_rate=0.05, num_leaves=64,
        subsample=0.8, subsample_freq=1, colsample_bytree=0.8,
        random_state=seed, n_jobs=THREADS, deterministic=True, force_col_wise=True,
        verbose=-1)
    model.fit(X_train, y_train)
    return model.predict_proba(X_val)[:, 1]


def run_xgboost(X_train, y_train, X_val, seed):
    model = xgb.XGBClassifier(
        n_estimators=400, learning_rate=0.05, max_leaves=64, max_depth=0,
        grow_policy="lossguide", tree_method="hist", enable_categorical=True,
        subsample=0.8, colsample_bytree=0.8,
        random_state=seed, n_jobs=THREADS)
    model.fit(X_train, y_train)
    return model.predict_proba(X_val)[:, 1]


def run_logistic(train, val, features):
    num_cols = [c for c in features if c not in CATEGORICAL]
    prep = ColumnTransformer([
        ("num", make_pipeline(SimpleImputer(strategy="median", add_indicator=True),
                              StandardScaler()), num_cols),
        ("cat", make_pipeline(SimpleImputer(strategy="constant", fill_value="missing"),
                              OneHotEncoder(handle_unknown="ignore")), CATEGORICAL),
    ])
    model = Pipeline([("prep", prep),
                      ("lr", LogisticRegression(max_iter=2000, class_weight="balanced"))])
    model.fit(train[features], train[TARGET])
    return model.predict_proba(val[features])[:, 1]


def main():
    print("loading data and building features...")
    df = add_normalized_d(load_data())
    train, val = df[df["split"] == "train"], df[df["split"] == "val"]
    y_train, y_val = train[TARGET], val[TARGET]
    normalized = [f"{c}_norm" if c in D_TO_NORMALIZE else c for c in FEATURES]
    print(f"train {len(train):,}, val {len(val):,}, frauds in val {int(y_val.sum()):,}")

    rows = []

    def record(model, d_cols, seed, scores, started):
        rows.append({"model": model, "D columns": d_cols, "seed": seed,
                     "PR-AUC": average_precision_score(y_val, scores),
                     "recall @ 1% FPR": recall_at_fpr(y_val, scores),
                     "minutes": (time.time() - started) / 60})
        r = rows[-1]
        print(f"  {model:<20} {d_cols:<10} seed {seed}: PR-AUC {r['PR-AUC']:.4f}, "
              f"recall@1%FPR {r['recall @ 1% FPR']:.4f} ({r['minutes']:.1f} min)")

    for d_cols, features in [("raw", FEATURES), ("normalized", normalized)]:
        X_train, X_val = tree_inputs(train, val, features)
        for seed in SEEDS:
            started = time.time()
            record("LightGBM", d_cols, seed, run_lightgbm(X_train, y_train, X_val, seed), started)
            started = time.time()
            record("XGBoost", d_cols, seed, run_xgboost(X_train, y_train, X_val, seed), started)
        started = time.time()
        record("Logistic regression", d_cols, "-", run_logistic(train, val, features), started)

    runs = pd.DataFrame(rows)
    summary = (runs.groupby(["model", "D columns"], sort=False)
               .agg(runs=("PR-AUC", "size"),
                    pr_auc_mean=("PR-AUC", "mean"), pr_auc_std=("PR-AUC", "std"),
                    recall_mean=("recall @ 1% FPR", "mean"),
                    recall_std=("recall @ 1% FPR", "std"))
               .fillna(0).round(4).reset_index())
    summary.columns = ["model", "D columns", "runs", "PR-AUC mean", "PR-AUC std",
                       "recall @ 1% FPR mean", "recall @ 1% FPR std"]
    print("\n", summary.to_string(index=False))

    with open(RESULTS / "e1_seeds.md", "w") as f:
        f.write("# E1 with 3 seeds: model choice and raw vs normalized D\n\n")
        f.write("65 features (request-time + customer history), trained on days 1-73, "
                "scored on days 74-109 "
                f"({int(y_val.sum()):,} frauds in {len(val):,} payments). "
                "Tree models use row/column sampling 0.8 and seeds "
                f"{', '.join(map(str, SEEDS))}; logistic regression has no randomness "
                "and runs once. Normalized D = transaction day - D for "
                f"{', '.join(D_TO_NORMALIZE)}.\n\n")
        f.write("## Summary (mean and standard deviation over seeds)\n\n")
        f.write(summary.to_markdown(index=False) + "\n\n")
        f.write("## Every run\n\n")
        f.write(runs.round(4).to_markdown(index=False) + "\n")
    print("\nsaved docs/results/e1_seeds.md")


if __name__ == "__main__":
    main()