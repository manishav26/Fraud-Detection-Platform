"""E2 extra: turn growing 'days since' D columns into fixed dates (day - D).

Compares LightGBM on days 74-109 (trained on days 1-73) with:
  1. raw D columns (as in the baseline)
  2. normalized D columns (day - D)
  3. normalized D columns + customer history
"""

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score

from e2_history import (BASE_FEATURES, HISTORY_FEATURES, RESULTS, TARGET, add_history,
                        add_proxy_id, caught_at_fpr, load_all, recall_at_fpr,
                        train_and_score)

# D8 and D9 are not plain day counts (D9 looks like a fraction of a day), so they stay raw.
D_TO_NORMALIZE = [f"D{i}" for i in range(1, 16) if i not in (8, 9)]


def add_normalized_d(df):
    """D_norm = transaction day - D: the date the counted event happened.
    It stays the same for one card over time, unlike the raw D which grows every day."""
    day = df["TransactionDT"] / 86400
    for col in D_TO_NORMALIZE:
        df[f"{col}_norm"] = np.floor(day - df[col])
    return df


def main():
    print("loading and building features...")
    df = add_normalized_d(add_history(add_proxy_id(load_all())))
    train, val = df[df["split"] == "train"], df[df["split"] == "val"]

    normalized = [f"{c}_norm" if c in D_TO_NORMALIZE else c for c in BASE_FEATURES]
    variants = [
        ("1. raw D (baseline)", BASE_FEATURES),
        ("2. normalized D", normalized),
        ("3. normalized D + history", normalized + HISTORY_FEATURES),
    ]

    rows, importance = [], None
    for name, features in variants:
        print(f"training {name}...")
        scores, importance = train_and_score(train, val, features)
        rows.append({"variant": name, "features": len(features),
                     "PR-AUC": round(average_precision_score(val[TARGET], scores), 4),
                     "recall @ 1% FPR": round(recall_at_fpr(val[TARGET], scores), 4),
                     "frauds caught @ 1% FPR": caught_at_fpr(val[TARGET], scores),
                     "frauds total": int(val[TARGET].sum())})

    results = pd.DataFrame(rows)
    print("\n", results.to_string(index=False))
    print("\nTop 15 features (variant 3):\n", importance.head(15).to_string())

    with open(RESULTS / "e2_dnorm.md", "w") as f:
        f.write("# E2 extra: normalized D columns\n\n")
        f.write("D_norm = transaction day - D for "
                f"{', '.join(D_TO_NORMALIZE)} (D8, D9 kept raw). "
                "Trained on days 1-73, evaluated on days 74-109.\n\n")
        f.write(results.to_markdown(index=False) + "\n\n")
        f.write("## Top 15 features (variant 3, split count)\n\n")
        f.write(importance.head(15).to_frame("importance").to_markdown() + "\n")
    print("\nsaved docs/results/e2_dnorm.md")


if __name__ == "__main__":
    main()