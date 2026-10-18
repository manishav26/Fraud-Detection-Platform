from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
PROCESSED = ROOT / "data" / "processed"
RESULTS = ROOT / "docs" / "results"

# The columns each proxy id is built from
VERSIONS = {
    "v1": ["card1", "addr1", "start_day"],
    "v2": ["card1", "card2", "card3", "card5", "addr1", "P_emaildomain", "start_day"],
}
NEEDED = ["TransactionDT", "isFraud", "D1", "card1", "card2", "card3", "card5",
          "addr1", "P_emaildomain"]


def load_all():
    """Load only the columns we need from all three splits, in time order."""
    parts = [pd.read_parquet(PROCESSED / f"{s}.parquet", columns=NEEDED)
             for s in ["train", "val", "stream"]]
    df = pd.concat(parts, ignore_index=True)
    return df.sort_values("TransactionDT").reset_index(drop=True)


def add_start_day(df):
    """Card start day = transaction day - D1 (days since the card was first used).
    It stays the same for every transaction of one card."""
    df["start_day"] = np.floor(df["TransactionDT"] / 86400 - df["D1"])
    return df


def build_id(df, cols):
    """Glue the chosen columns into one text id, e.g. '9500|204.0|12.0'."""
    return df[cols].astype(str).agg("|".join, axis=1)


def id_stats(df, id_col):
    """How useful is this id for per-user features?"""
    counts = df.groupby(id_col).size()                 # transactions per id
    earlier = df.groupby(id_col).cumcount()            # earlier txns of the same id
    return {
        "number of ids": counts.size,
        "median txns per id": counts.median(),
        "90th pct txns per id": counts.quantile(0.9),
        "max txns per id": counts.max(),
        "share of ids with only 1 txn": round((counts == 1).mean(), 3),
        "share of rows with >=3 earlier txns": round((earlier >= 3).mean(), 3),
    }


def main():
    df = add_start_day(load_all())
    print(f"rows: {len(df)}   D1 missing: {df['D1'].isna().mean():.1%}")

    results = {}
    for name, cols in VERSIONS.items():
        df[name] = build_id(df, cols)
        results[name] = id_stats(df, name)

    table = pd.DataFrame(results)
    print(table)

    RESULTS.mkdir(parents=True, exist_ok=True)
    with open(RESULTS / "proxy_id.md", "w") as f:
        f.write("# Proxy user id check\n\n")
        for name, cols in VERSIONS.items():
            f.write(f"- **{name}**: {' + '.join(cols)}\n")
        f.write("\n" + table.to_markdown() + "\n")
    print("saved docs/results/proxy_id.md")
    print("\nTop 5 biggest v1 ids:")
    print(df["v1"].value_counts().head(5))

if __name__ == "__main__":
    main()
   