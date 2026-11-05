"""Per-user history features. Every feature uses ONLY earlier transactions of the same user.

Two versions of the same logic:
- add_history(df):      offline, for a whole table at once (training)
- history_for_one(...): online, for one new transaction given the user's past (live scoring)
A test checks that both give the same values (train/serve parity).
"""

import math

import numpy as np
import pandas as pd

WINDOWS = {"1h": 3600, "24h": 86400, "30d": 30 * 86400}
NOVELTY_OF = ["P_emaildomain", "DeviceType", "ProductCD"]
HISTORY_FEATURES = (["n_prev_txns", "txn_count_1h", "txn_count_24h", "txn_count_30d",
                     "amt_sum_24h", "amt_mean_30d", "amt_std_30d", "amount_zscore",
                     "secs_since_last"] + [f"new_{c}" for c in NOVELTY_OF])
MIN_STD = 1e-6   # a spread below this is float rounding, not real: treat it as 0

def add_history(df, time_col="TransactionDT", amount_col="TransactionAmt"):
    """Offline: add HISTORY_FEATURES to a table that already has `uid` and `uid_known`."""
    df = df.sort_values(["uid", time_col], kind="stable").reset_index(drop=True)
    g = pd.factorize(df["uid"])[0].astype(np.int64)
    t = df[time_col].to_numpy(np.int64)
    amt = df[amount_col].to_numpy(float)
    idx = np.arange(len(df))

    key = g * np.int64(10**9) + t                       # sorted by user, then time
    cs = np.concatenate([[0.0], np.cumsum(amt)])        # running sum for the 24 h total
    n_before = df.groupby(g).cumcount().to_numpy()

    for name, seconds in WINDOWS.items():
        left = np.searchsorted(key, key - seconds, side="left")   # first row inside window
        count = idx - left                                         # earlier rows in window
        df[f"txn_count_{name}"] = count
        if name == "24h":
            df["amt_sum_24h"] = cs[idx] - cs[left]
        if name == "30d":
            # Mean and spread straight from each window, the same way history_for_one does.
            # (Running sums of squares over the whole table lost precision: a user who always
            # pays the same amount got a tiny fake spread and a zscore in the millions.)
            mean = np.full(len(df), np.nan)
            std = np.full(len(df), np.nan)
            for i in np.flatnonzero(count > 0):
                window = amt[left[i]:i]
                mean[i] = window.mean()
                if count[i] > 1:
                    std[i] = window.std()
            std = np.where(std < MIN_STD, 0.0, std)          # missing stays missing
            df["amt_mean_30d"], df["amt_std_30d"] = mean, std
            with np.errstate(divide="ignore", invalid="ignore"):
                df["amount_zscore"] = np.where(std > 0, (amt - mean) / std, np.nan)

    prev_t = np.roll(t, 1).astype(float)
    df["secs_since_last"] = np.where(n_before > 0, t - prev_t, np.nan)
    df["n_prev_txns"] = n_before
    for col in NOVELTY_OF:
        first_seen = df.groupby(["uid", col], dropna=False).cumcount() == 0
        df[f"new_{col}"] = (first_seen & (n_before > 0)).astype(int)

    df[HISTORY_FEATURES] = df[HISTORY_FEATURES].astype(float)
    df.loc[~df["uid_known"], HISTORY_FEATURES] = np.nan   # unknown user: no trusted history
    return df.sort_values(time_col, kind="stable").reset_index(drop=True)


def _missing(v):
    return v is None or (isinstance(v, float) and math.isnan(v))


def _same(a, b):
    """Equality that treats missing values as equal (matches pandas dropna=False)."""
    return (_missing(a) and _missing(b)) or a == b


def history_for_one(past, t, amount, values, known=True):
    """Online: features for ONE new transaction.

    past:   list of earlier transactions of this user, each a dict with "t", "amount"
            and the NOVELTY_OF columns, in time order
    t, amount, values: the new transaction (values = dict of the NOVELTY_OF columns)
    """
    if not known:
        return {name: float("nan") for name in HISTORY_FEATURES}

    out = {"n_prev_txns": float(len(past))}
    for name, seconds in WINDOWS.items():
        out[f"txn_count_{name}"] = float(sum(1 for p in past if p["t"] >= t - seconds))
    out["amt_sum_24h"] = float(sum(p["amount"] for p in past if p["t"] >= t - WINDOWS["24h"]))

    last30 = [p["amount"] for p in past if p["t"] >= t - WINDOWS["30d"]]
    mean = float(np.mean(last30)) if last30 else float("nan")
    std = float(np.std(last30)) if len(last30) > 1 else float("nan")
    std = 0.0 if std < MIN_STD else std          # same rounding rule as add_history
    out["amt_mean_30d"], out["amt_std_30d"] = mean, std
    out["amount_zscore"] = (amount - mean) / std if std > 0 else float("nan")
    out["secs_since_last"] = float(t - past[-1]["t"]) if past else float("nan")
    for col in NOVELTY_OF:
        seen = any(_same(p[col], values[col]) for p in past)
        out[f"new_{col}"] = float(bool(past) and not seen)
    return out