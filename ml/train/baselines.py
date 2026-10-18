from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score, roc_curve
from sklearn.pipeline import Pipeline, make_pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

ROOT = Path(__file__).resolve().parents[2]
PROCESSED = ROOT / "data" / "processed"
RESULTS = ROOT / "docs" / "results"

# Request-time features: things known from the payment itself, no customer history
NUMERIC = (["TransactionAmt", "card1", "card2", "card3", "card5", "addr1", "addr2", "dist1"]
           + [f"C{i}" for i in range(1, 15)] + [f"D{i}" for i in range(1, 16)])
CATEGORICAL = (["ProductCD", "card4", "card6", "P_emaildomain", "R_emaildomain", "DeviceType"]
               + [f"M{i}" for i in range(1, 10)])
TARGET = "isFraud"


def load(split):
    cols = NUMERIC + CATEGORICAL + ["TransactionDT", TARGET]
    df = pd.read_parquet(PROCESSED / f"{split}.parquet", columns=cols)
    df["hour"] = (df["TransactionDT"] // 3600) % 24     # hour of day
    df["log_amount"] = np.log1p(df["TransactionAmt"])    # amounts are very skewed
    return df


def recall_at_fpr(y_true, scores, fpr_target=0.01):
    """Share of fraud caught when only `fpr_target` of normal payments are wrongly flagged."""
    fpr, tpr, _ = roc_curve(y_true, scores)
    return float(np.interp(fpr_target, fpr, tpr))


def evaluate(name, y_true, scores):
    return {
        "model": name,
        "PR-AUC": round(average_precision_score(y_true, scores), 4),
        "recall @ 1% FPR": round(recall_at_fpr(y_true, scores, 0.01), 4),
        "recall @ 2% FPR": round(recall_at_fpr(y_true, scores, 0.02), 4),
        "recall @ 5% FPR": round(recall_at_fpr(y_true, scores, 0.05), 4),
        "ROC-AUC": round(roc_auc_score(y_true, scores), 4),
    }


def confusion_at_fpr(name, y_true, scores, fpr_target):
    """Real counts on validation when we flag `fpr_target` of normal payments."""
    y_true = np.asarray(y_true)
    scores = np.asarray(scores)
    normal_scores = scores[y_true == 0]
    # cut-off chosen so that fpr_target of normal payments score above it
    threshold = np.quantile(normal_scores, 1 - fpr_target)
    flagged = scores > threshold

    caught = int(((y_true == 1) & flagged).sum())
    missed = int(((y_true == 1) & ~flagged).sum())
    false_alarms = int(((y_true == 0) & flagged).sum())
    approved_ok = int(((y_true == 0) & ~flagged).sum())
    total = len(y_true)

    return {
        "model": name,
        "false-alarm rate": f"{fpr_target:.0%}",
        "fraud total": caught + missed,
        "caught": caught,
        "missed": missed,
        "false alarms": false_alarms,
        "honest approved": approved_ok,
        "precision": round(caught / max(caught + false_alarms, 1), 3),
        "accuracy": round((caught + approved_ok) / total, 4),
    }


def logistic_regression(train, val, num_cols, cat_cols):
    prep = ColumnTransformer([
        ("num", make_pipeline(SimpleImputer(strategy="median", add_indicator=True),
                              StandardScaler()), num_cols),
        ("cat", make_pipeline(SimpleImputer(strategy="constant", fill_value="missing"),
                              OneHotEncoder(handle_unknown="ignore")), cat_cols),
    ])
    model = Pipeline([("prep", prep),
                      ("lr", LogisticRegression(max_iter=2000, class_weight="balanced"))])
    model.fit(train[num_cols + cat_cols], train[TARGET])
    return model.predict_proba(val[num_cols + cat_cols])[:, 1]


def lightgbm(train, val, num_cols, cat_cols):
    X_train = train[num_cols + cat_cols].copy()
    X_val = val[num_cols + cat_cols].copy()
    for c in cat_cols:   # categories learned from train only
        X_train[c] = X_train[c].astype("category")
        X_val[c] = pd.Categorical(X_val[c], categories=X_train[c].cat.categories)
    model = lgb.LGBMClassifier(n_estimators=400, learning_rate=0.05, num_leaves=64,
                               random_state=42, verbose=-1)
    model.fit(X_train, train[TARGET])
    importance = pd.Series(model.feature_importances_, index=X_train.columns)
    return model.predict_proba(X_val)[:, 1], importance.sort_values(ascending=False)


def main():
    train, val = load("train"), load("val")
    num_cols = [c for c in NUMERIC if c != "TransactionAmt"] + ["log_amount", "hour"]
    cat_cols = CATEGORICAL
    print(f"train {train.shape}, val {val.shape}, features {len(num_cols) + len(cat_cols)}")

    print("training logistic regression...")
    lr_scores = logistic_regression(train, val, num_cols, cat_cols)
    print("training LightGBM...")
    lgb_scores, importance = lightgbm(train, val, num_cols, cat_cols)

    # Scores
    results = pd.DataFrame([evaluate("Logistic regression", val[TARGET], lr_scores),
                            evaluate("LightGBM", val[TARGET], lgb_scores)])
    print("\n", results.to_string(index=False))
    print("\nTop 15 LightGBM features:\n", importance.head(15).to_string())

    # Real counts on validation at different false-alarm rates
    rows = []
    for name, scores in [("Logistic regression", lr_scores), ("LightGBM", lgb_scores)]:
        for fpr in [0.01, 0.02, 0.05]:
            rows.append(confusion_at_fpr(name, val[TARGET], scores, fpr))
    counts = pd.DataFrame(rows)
    never_fraud_acc = 1 - val[TARGET].mean()
    print("\nCounts on validation (days 74-109):\n", counts.to_string(index=False))
    print(f"\n'Never fraud' model accuracy: {never_fraud_acc:.4f}")

    # Save everything to docs/results/e1.md
    RESULTS.mkdir(parents=True, exist_ok=True)
    with open(RESULTS / "e1.md", "w") as f:
        f.write("# E1 baseline (request-time features only)\n\n")
        f.write("Trained on days 1-73, evaluated on validation days 74-109. "
                f"Fraud rate in validation: {val[TARGET].mean():.2%}.\n\n")
        f.write("## Scores\n\n")
        f.write(results.to_markdown(index=False) + "\n\n")
        f.write("## Counts on validation\n\n")
        f.write(counts.to_markdown(index=False) + "\n\n")
        f.write(f"A model that never predicts fraud would have accuracy {never_fraud_acc:.4f}, "
                "so accuracy is not a useful measure here.\n\n")
        f.write("## Top 15 LightGBM features (split count)\n\n")
        f.write(importance.head(15).to_frame("importance").to_markdown() + "\n")
    print("\nsaved docs/results/e1.md")


if __name__ == "__main__":
    main()