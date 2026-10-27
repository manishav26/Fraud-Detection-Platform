"""Label simulator: fraud answers arrive late, as in a real bank.

The dataset already contains every fraud label, but a real bank only learns that a payment
was fraud days or weeks later (when the customer disputes it). This module hides each label
until `delay_days` simulated days after its transaction, so experiments never train on
answers that would not have been known yet.

Times are in TransactionDT seconds (the dataset's own clock), never real wall-clock time.
"""

import numpy as np
import pandas as pd

SECONDS_PER_DAY = 86_400
DEFAULT_DELAY_DAYS = 14


def release_time(transaction_dt, delay_days=DEFAULT_DELAY_DAYS):
    """When the label of a transaction becomes known (in TransactionDT seconds)."""
    if delay_days < 0:
        raise ValueError("delay_days must be 0 or more")
    return np.asarray(transaction_dt) + delay_days * SECONDS_PER_DAY


def known_labels(df, now, delay_days=DEFAULT_DELAY_DAYS, time_col="TransactionDT"):
    """Rows whose label is known at simulated time `now`.

    Use this whenever the experiment needs labels (training, drift checks):
    it only returns transactions that happened at least `delay_days` before `now`.
    """
    released = release_time(df[time_col], delay_days) <= now
    return df[released]


def release_schedule(df, delay_days=DEFAULT_DELAY_DAYS, time_col="TransactionDT",
                     id_col="TransactionID", label_col="isFraud"):
    """Every label with the simulated day it is released, in release order.

    This is what the platform's label topic receives during a replay
    (one message per transaction: id, label, release time).
    """
    out = pd.DataFrame({
        id_col: df[id_col].to_numpy(),
        label_col: df[label_col].to_numpy(),
        "txn_time": df[time_col].to_numpy(),
        "release_time": release_time(df[time_col], delay_days),
    })
    out["release_day"] = out["release_time"] // SECONDS_PER_DAY
    return out.sort_values("release_time", kind="stable").reset_index(drop=True)


def main():
    """Small demo on the real stream split: how many labels are known on a few days."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    cols = ["TransactionID", "TransactionDT", "isFraud"]
    stream = pd.read_parquet(root / "data" / "processed" / "stream.parquet", columns=cols)
    first_day = int(stream["TransactionDT"].min() // SECONDS_PER_DAY)
    print(f"stream starts on day {first_day}, {len(stream)} transactions\n")
    print(f"{'day':>5} {'seen so far':>12} {'labels known (L=14)':>20} {'share known':>12}")
    for day in [first_day + k for k in (0, 7, 14, 21, 35, 60)]:
        now = (day + 1) * SECONDS_PER_DAY            # end of that day
        seen = int((stream["TransactionDT"] < now).sum())
        known = len(known_labels(stream, now))
        share = known / seen if seen else 0
        print(f"{day:>5} {seen:>12} {known:>20} {share:>12.1%}")


if __name__ == "__main__":
    main()