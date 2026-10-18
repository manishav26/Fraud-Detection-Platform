
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]      # the repo folder
RAW = ROOT / "data" / "raw" / "ieee-cis"
OUT = ROOT / "data" / "processed"


def load_data():
    """Read both CSVs, join them, add a day number, sort by time."""
    tx = pd.read_csv(RAW / "train_transaction.csv")
    ident = pd.read_csv(RAW / "train_identity.csv")
    df = tx.merge(ident, on="TransactionID", how="left")
    df["day"] = df["TransactionDT"] // 86400
    return df.sort_values("TransactionDT").reset_index(drop=True)


def time_split(df, train_frac=0.4, val_frac=0.2):
    """Split by whole days: first 40% train, next 20% validation, rest stream."""
    days = sorted(df["day"].unique())
    n = len(days)
    train_end = days[round(n * train_frac) - 1]
    val_end = days[round(n * (train_frac + val_frac)) - 1]

    train = df[df["day"] <= train_end]
    val = df[(df["day"] > train_end) & (df["day"] <= val_end)]
    stream = df[df["day"] > val_end]
    return train, val, stream


def check_no_overlap(train, val, stream):
    """Fail if any later split contains a time at or before an earlier one."""
    assert train["TransactionDT"].max() < val["TransactionDT"].min(), "train overlaps val"
    assert val["TransactionDT"].max() < stream["TransactionDT"].min(), "val overlaps stream"
    print("OK: no time overlap between splits")


def main():
    df = load_data()
    train, val, stream = time_split(df)
    check_no_overlap(train, val, stream)
    assert len(train) + len(val) + len(stream) == len(df), "rows lost"

    OUT.mkdir(parents=True, exist_ok=True)
    for name, part in [("train", train), ("val", val), ("stream", stream)]:
        part.to_parquet(OUT / f"{name}.parquet", index=False)
        print(f"{name:7s} rows={len(part):7d}  share={len(part)/len(df):.1%}  "
              f"fraud={part['isFraud'].mean():.4f}  days={part['day'].min()}-{part['day'].max()}")


if __name__ == "__main__":
    main()