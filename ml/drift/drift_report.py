"""Drift on the stream days (110-182) compared with training (days 1-73).

1. Features: PSI and KS per feature, for the whole stream and week by week.
2. Model score: PSI of the score, plus ADWIN on the score stream (no labels needed).
3. Model errors: ADWIN on |label - score|, fed when each label arrives (14 days late).
4. Does it hurt? Share of fraud flagged (review or decline) week by week.

    python -m ml.drift.drift_report      # about 1-2 minutes
"""

from pathlib import Path

import pandas as pd

from ml.drift.detectors import ADWIN, PSI_MAJOR, PSI_MODERATE, feature_drift, psi
from ml.model import FraudModel
from ml.train.calibrate import CATEGORICAL, FEATURES, RESULTS, TARGET, load_data

ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = ROOT / "models" / "fraud-lgbm-v1"
LABEL_DELAY_DAYS = 14


def adwin_days(values, days, gap=7):
    """Run ADWIN over values in order; return the first day of each alarm episode.

    ADWIN often fires several times in a row for one change (it keeps trimming its window),
    so alarms less than `gap` days after the previous one count as the same episode.
    """
    detector = ADWIN()
    for v in values:
        detector.update(v)
    episodes, last = [], None
    for i in detector.drifts:
        day = int(days[i - 1])
        if last is None or day - last > gap:
            episodes.append(day)
        last = day
    return episodes


def main():
    model = FraudModel.load(MODEL_DIR)
    print("loading data and building features...")
    df = load_data()
    train = df[df["split"] == "train"]
    stream = df[df["split"] == "stream"].sort_values("TransactionDT", kind="stable")
    stream = stream.assign(day=stream["TransactionDT"] // 86400)
    first_day = int(stream["day"].min())
    stream = stream.assign(week=(stream["day"] - first_day) // 7 + 1)
    print(f"train {len(train):,} payments, stream {len(stream):,} payments "
          f"(days {first_day}-{int(stream['day'].max())})")

    print("scoring train and stream...")
    train_score = model.predict_frame(train)
    stream = stream.assign(score=model.predict_frame(stream))
    stream = stream.assign(flagged=stream["score"] >= model.meta["thresholds"]["review_threshold"])

    # 1. features: whole stream vs train
    overall = feature_drift(train, stream, FEATURES, categorical=CATEGORICAL)
    print("\nfeatures with the most drift (whole stream vs train):")
    print(overall.head(15).round(4).to_string(index=False))

    # 1b + 4. week by week
    weekly = []
    for week, part in stream.groupby("week"):
        table = feature_drift(train, part, FEATURES, categorical=CATEGORICAL)
        frauds = part[part[TARGET] == 1]
        weekly.append({
            "week": int(week),
            "days": f"{int(part['day'].min())}-{int(part['day'].max())}",
            "payments": len(part),
            "features moderate": int((table["level"] == "moderate").sum()),
            "features major": int((table["level"] == "major").sum()),
            "worst feature": f"{table.loc[0, 'feature']} ({table.loc[0, 'PSI']:.2f})",
            "score PSI": psi(train_score, part["score"]),
            "fraud rate": part[TARGET].mean(),
            "fraud flagged": frauds["flagged"].mean() if len(frauds) else float("nan"),
        })
    weekly = pd.DataFrame(weekly)
    print("\nweek by week:")
    print(weekly.round(4).to_string(index=False))

    # 2-3. ADWIN on the score (immediately) and on the error (when the label arrives)
    days = stream["day"].to_numpy()
    score_drifts = adwin_days(stream["score"].to_numpy(), days)
    error = (stream[TARGET] - stream["score"]).abs().to_numpy()
    error_drifts = [d + LABEL_DELAY_DAYS for d in adwin_days(error, days)]
    frauds = stream[stream[TARGET] == 1]
    missed = (~frauds["flagged"]).astype(float).to_numpy()   # 1 = fraud we let through
    missed_drifts = [d + LABEL_DELAY_DAYS
                     for d in adwin_days(missed, frauds["day"].to_numpy())]
    print(f"ADWIN on missed fraud (label {LABEL_DELAY_DAYS} days late): alarm on days "
          f"{missed_drifts or 'none'}")
    print(f"\nADWIN on score: drift on days {score_drifts or 'none'}")
    print(f"ADWIN on error (label {LABEL_DELAY_DAYS} days late): alarm on days "
          f"{error_drifts or 'none'}")

    n_major = int((overall["level"] == "major").sum())
    n_moderate = int((overall["level"] == "moderate").sum())
    with open(RESULTS / "drift.md", "w") as f:
        f.write("# Drift on the stream days\n\n")
        f.write(f"Reference: training days 1-73 ({len(train):,} payments). Compared: stream days "
                f"{first_day}-{int(stream['day'].max())} ({len(stream):,} payments), the period "
                "the live system replays. Model `fraud-lgbm-v1`. PSI levels: < "
                f"{PSI_MODERATE} stable, {PSI_MODERATE}-{PSI_MAJOR} moderate, > {PSI_MAJOR} major."
                "\n\n")
        f.write(f"## Features: whole stream vs training ({n_major} major, {n_moderate} moderate "
                f"out of {len(FEATURES)})\n\n")
        f.write(overall.head(15).round(4).to_markdown(index=False) + "\n\n")
        f.write("KS is only for numeric features. With this many payments almost any "
                "difference has a tiny p-value, so the KS statistic (size of the gap) and PSI "
                "are the useful numbers.\n\n")
        f.write("## Week by week\n\n")
        f.write(weekly.round(4).to_markdown(index=False) + "\n\n")
        f.write("`fraud flagged` = share of real fraud sent to review or decline that week.\n\n")
        f.write("## ADWIN (streaming)\n\n")
        f.write("| Stream watched | Needs labels | Drift detected on days |\n|---|---|---|\n")
        f.write(f"| model score | no | {', '.join(map(str, score_drifts)) or 'none'} |\n")
        f.write(f"| model error \\|label - score\\| | yes, {LABEL_DELAY_DAYS} days late | "
                f"{', '.join(map(str, error_drifts)) or 'none'} |\n")
        f.write(f"| missed fraud (1 per real fraud) | yes, {LABEL_DELAY_DAYS} days late | "
                f"{', '.join(map(str, missed_drifts)) or 'none'} |\n\n")
        f.write("Error alarms are reported on the day the label arrives (payment day + "
                f"{LABEL_DELAY_DAYS}), which is when a live system could raise them.\n")
    print("\nsaved docs/results/drift.md")


if __name__ == "__main__":
    main()