"""E2 first pass: does customer history improve LightGBM?

Builds per-user history features using ONLY earlier transactions of the same proxy user,
then trains LightGBM twice (without / with history) on days 1-73 and scores days 74-109.
"""

from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, roc_curve

ROOT = Path(__file__).resolve().parents[2]
PROCESSED = ROOT / "data" / "processed"
RESULTS = ROOT / "docs" / "results"

NUMERIC = (["card1", "card2", "card3", "card5", "addr1", "addr2", "dist1"]
           + [f"C{i}" for i in range(1, 15)] + [f"D{i}" for i in range(1, 16)])
CATEGORICAL = (["ProductCD", "card4", "card6", "P_emaildomain", "R_emaildomain", "DeviceType"]
               + [f"M{i}" for i in range(1, 10)])
TARGET = "isFraud"
BASE_FEATURES = NUMERIC + ["log_amount", "hour"] + CATEGORICAL
NOVELTY_OF = ["P_emaildomain", "DeviceType", "ProductCD"]
HISTORY_FEATURES = (["n_prev_txns", "txn_count_1h", "txn_count_24h", "txn_count_30d",
                     "amt_sum_24h", "amt_mean_30d", "amt_std_30d", "amount_zscore",
                     "secs_since_last"] + [f"new_{c}" for c in NOVELTY_OF])


def load_all():
    cols = NUMERIC + CATEGORICAL + ["TransactionDT", "TransactionAmt", TARGET]
    parts = []
    for split in ["train", "val", "stream"]:
        part = pd.read_parquet(PROCESSED / f"{split}.parquet", columns=cols)
        part["split"] = split
        parts.append(part)
    df = pd.concat(parts, ignore_index=True)
    df["hour"] = (df["TransactionDT"] // 3600) % 24
    df["log_amount"] = np.log1p(df["TransactionAmt"])
    return df


def add_proxy_id(df):
    """Proxy user id v1: card1 | addr1 | card start day."""
    start_day = np.floor(df["TransactionDT"] / 86400 - df["D1"])
    parts = [df["card1"], df["addr1"], start_day]
    parts = [p.astype("string").fillna("NA") for p in parts]   # missing -> "NA", never blank
    df["uid"] = parts[0] + "|" + parts[1] + "|" + parts[2]
    df["uid_known"] = df["addr1"].notna() & start_day.notna()
    return df


def add_history(df):
    """Per-user features from EARLIER transactions only (no peeking at the future)."""
    df = df.sort_values(["uid", "TransactionDT"], kind="stable").reset_index(drop=True)
    g = pd.factorize(df["uid"])[0].astype(np.int64)        # user number, in sorted order
    t = df["TransactionDT"].to_numpy(np.int64)
    amt = df["TransactionAmt"].to_numpy(float)
    idx = np.arange(len(df))

    key = g * np.int64(10**9) + t                           # sorted: user first, then time
    cs = np.concatenate([[0.0], np.cumsum(amt)])            # running sums for fast windows
    cs2 = np.concatenate([[0.0], np.cumsum(amt ** 2)])
    n_before = df.groupby(g).cumcount().to_numpy()          # earlier txns of this user

    for name, seconds in [("1h", 3600), ("24h", 86400), ("30d", 30 * 86400)]:
        left = np.searchsorted(key, key - seconds, side="left")  # first row inside window
        count = idx - left                                        # earlier rows in window
        df[f"txn_count_{name}"] = count
        if name == "24h":
            df["amt_sum_24h"] = cs[idx] - cs[left]
        if name == "30d":
            total, total2 = cs[idx] - cs[left], cs2[idx] - cs2[left]
            with np.errstate(divide="ignore", invalid="ignore"):
                mean = total / count
                std = np.sqrt(np.maximum(total2 / count - mean ** 2, 0))
            mean = np.where(count > 0, mean, np.nan)
            std = np.where(count > 1, std, np.nan)
            df["amt_mean_30d"], df["amt_std_30d"] = mean, std
            with np.errstate(divide="ignore", invalid="ignore"):
                df["amount_zscore"] = np.where(std > 0, (amt - mean) / std, np.nan)

    prev_t = np.roll(t, 1).astype(float)
    df["secs_since_last"] = np.where(n_before > 0, t - prev_t, np.nan)
    df["n_prev_txns"] = n_before
    for col in NOVELTY_OF:   # first time this user is seen with this value?
        first_seen = df.groupby(["uid", col], dropna=False).cumcount() == 0
        df[f"new_{col}"] = (first_seen & (n_before > 0)).astype(int)

    # unknown users (id built from missing values): no trustworthy history
    df.loc[~df["uid_known"], HISTORY_FEATURES] = np.nan
    return df.sort_values("TransactionDT", kind="stable").reset_index(drop=True)


def recall_at_fpr(y, s, fpr_target=0.01):
    fpr, tpr, _ = roc_curve(y, s)
    return float(np.interp(fpr_target, fpr, tpr))


def caught_at_fpr(y, s, fpr_target=0.01):
    y, s = np.asarray(y), np.asarray(s)
    threshold = np.quantile(s[y == 0], 1 - fpr_target)
    return int(((y == 1) & (s > threshold)).sum())


def train_and_score(train, val, features):
    X_train, X_val = train[features].copy(), val[features].copy()
    for c in CATEGORICAL:
        X_train[c] = X_train[c].astype("category")
        X_val[c] = pd.Categorical(X_val[c], categories=X_train[c].cat.categories)
    model = lgb.LGBMClassifier(n_estimators=400, learning_rate=0.05, num_leaves=64,
                               random_state=42, verbose=-1)
    model.fit(X_train, train[TARGET])
    importance = pd.Series(model.feature_importances_, index=features)
    return model.predict_proba(X_val)[:, 1], importance.sort_values(ascending=False)


def main():
    print("loading and building history features...")
    df = add_history(add_proxy_id(load_all()))
    train, val = df[df["split"] == "train"], df[df["split"] == "val"]
    print(f"train {len(train)}, val {len(val)}, "
          f"rows with known user: {df['uid_known'].mean():.1%}")

    rows, importance = [], None
    for name, features in [("LightGBM, no history", BASE_FEATURES),
                           ("LightGBM + history", BASE_FEATURES + HISTORY_FEATURES)]:
        print(f"training {name}...")
        scores, importance = train_and_score(train, val, features)
        rows.append({"model": name, "features": len(features),
                     "PR-AUC": round(average_precision_score(val[TARGET], scores), 4),
                     "recall @ 1% FPR": round(recall_at_fpr(val[TARGET], scores), 4),
                     "frauds caught @ 1% FPR": caught_at_fpr(val[TARGET], scores),
                     "frauds total": int(val[TARGET].sum())})

    results = pd.DataFrame(rows)
    print("\n", results.to_string(index=False))
    print("\nTop 15 features (with history):\n", importance.head(15).to_string())

    RESULTS.mkdir(parents=True, exist_ok=True)
    with open(RESULTS / "e2.md", "w") as f:
        f.write("# E2 first pass: does customer history help?\n\n")
        f.write("Trained on days 1-73, evaluated on days 74-109. History features use only "
                "earlier transactions of the same proxy user (v1).\n\n")
        f.write(results.to_markdown(index=False) + "\n\n")
        f.write("## Top 15 features (with history, split count)\n\n")
        f.write(importance.head(15).to_frame("importance").to_markdown() + "\n")
    print("\nsaved docs/results/e2.md")


if __name__ == "__main__":
    main()