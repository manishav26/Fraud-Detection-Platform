"""Proxy user id v1: card1 | addr1 | card start day (see data/README.md)."""

import numpy as np


def add_proxy_id(df):
    """Add `uid` and `uid_known`. Missing parts become "NA" and mark the user as unknown."""
    start_day = np.floor(df["TransactionDT"] / 86400 - df["D1"])
    parts = [df["card1"], df["addr1"], start_day]
    parts = [p.astype("string").fillna("NA") for p in parts]
    df["uid"] = parts[0] + "|" + parts[1] + "|" + parts[2]
    df["uid_known"] = df["addr1"].notna() & start_day.notna()
    return df