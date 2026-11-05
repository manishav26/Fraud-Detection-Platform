"""E3: is drift-triggered retraining worth it? Static vs periodic vs drift-triggered.

An offline simulator walks the stream days (110-182) one day at a time, with no Kubernetes:
  - each day's payments are scored by the strategy's current model;
  - labels arrive 14 days late (ml/labels/label_sim.py), and only released labels are ever used
    for training or for the drift detector;
  - analysts review the top 2% of each day's scores (REVIEW_CAPACITY), so "missed fraud" means
    a fraud outside that day's top 2%.

Strategies (all start from the same model, trained on days 1-73 like v1):
  static     never retrains
  periodic   retrains every 30 days on the last 60 labelled days
  drift      retrains when ADWIN on the missed-fraud stream fires (labels 14 days late), with a
             7-day cooldown and at most 1 retrain per week; PSI on the score only logs warnings

    python -m ml.experiments.e3_retraining            # 3 seeds, about 5-10 minutes
Writes docs/results/e3.md and docs/figures/e3_weekly.png.
"""

import argparse
import time

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, roc_curve

from ml.drift.detectors import ADWIN, PSI_MAJOR, psi
from ml.labels.label_sim import SECONDS_PER_DAY, known_labels
from ml.train.calibrate import (
    FIGURES,
    RESULTS,
    REVIEW_CAPACITY,
    TARGET,
    load_data,
    model_params,
    to_model_input,
)

LABEL_DELAY_DAYS = 14
WINDOW_DAYS = 60          # retrain on the last 60 labelled days (plan: sliding window)
PERIOD_DAYS = 30          # periodic strategy
COOLDOWN_DAYS = 7         # drift strategy: no retrain within 7 days of the last one
WEEKLY_CAP = 1            # drift strategy: at most 1 retrain per 7 days
INITIAL_TRAIN_DAYS = (1, 73)
SEEDS = [1, 2, 3]
STRATEGIES = ["static", "periodic", "drift"]


# ---------- small pure pieces (tested in tests/test_e3.py) ----------

def training_rows(df, day, delay_days=LABEL_DELAY_DAYS, window_days=WINDOW_DAYS):
    """Rows a retrain at the END of `day` may use: labels already released, last window_days.

    Uses label_sim.known_labels, so a payment whose label is not yet known can never be used.
    """
    now = (day + 1) * SECONDS_PER_DAY - 1                # last second of the day
    known = known_labels(df, now, delay_days)
    newest_day = known["day"].max()
    return known[known["day"] > newest_day - window_days]


def capacity_flags(scores, capacity=REVIEW_CAPACITY):
    """True for the top `capacity` share of one day's scores (what analysts can review)."""
    scores = np.asarray(scores)
    k = max(1, round(capacity * len(scores)))
    flagged = np.zeros(len(scores), dtype=bool)
    flagged[np.argsort(-scores, kind="stable")[:k]] = True
    return flagged


class Periodic:
    def __init__(self, first_day, every=PERIOD_DAYS):
        self.first_day, self.every = first_day, every

    def wants_retrain(self, day, **_):
        return day > self.first_day and (day - self.first_day) % self.every == 0


class DriftTriggered:
    """Retrain when ADWIN on missed fraud fires, respecting a cooldown and a weekly cap."""

    def __init__(self, cooldown=COOLDOWN_DAYS, weekly_cap=WEEKLY_CAP):
        self.cooldown, self.weekly_cap = cooldown, weekly_cap
        self.detector = ADWIN()
        self.retrain_days = []
        self.alarms = []              # every ADWIN alarm, used or not
        self.pending = False

    def observe(self, day, missed):
        """Feed the missed-fraud outcomes whose labels were released today."""
        for m in missed:
            if self.detector.update(m):
                self.alarms.append(int(day))
                self.pending = True

    def wants_retrain(self, day, **_):
        if not self.pending:
            return False
        if self.retrain_days and day - self.retrain_days[-1] < self.cooldown:
            return False
        return sum(1 for d in self.retrain_days if day - d < 7) < self.weekly_cap

    def retrained(self, day):
        self.retrain_days.append(day)
        self.pending = False
        self.detector = ADWIN()       # the new model makes different mistakes: start fresh


# ---------- training and scoring ----------

def train(rows, seed):
    """LightGBM with the tuned v1 settings. Returns (model, categories, seconds)."""
    import lightgbm as lgb

    start = time.perf_counter()
    X, categories = to_model_input(rows)
    model = lgb.LGBMClassifier(**{**model_params(), "random_state": seed}, n_jobs=8,
                               deterministic=True, force_col_wise=True, verbose=-1)
    model.fit(X, rows[TARGET])
    return (model, categories), time.perf_counter() - start


def score(trained, rows):
    model, categories = trained
    return model.predict_proba(to_model_input(rows, categories)[0])[:, 1]


def recall_at_fpr(y, s, fpr_target=0.01):
    if y.sum() == 0 or y.sum() == len(y):
        return float("nan")
    fpr, tpr, _ = roc_curve(y, s)
    return float(np.interp(fpr_target, fpr, tpr))


# ---------- the simulator ----------

def simulate(df, strategy, seed, initial):
    """Walk the stream day by day. Returns (scored stream rows, retrain log, PSI warning days)."""
    stream = df[df["split"] == "stream"]
    days = sorted(stream["day"].unique())
    trained, train_seconds = initial
    ref_scores = score(trained, df[df["day"].between(*INITIAL_TRAIN_DAYS)])
    policy = {"static": None, "periodic": Periodic(days[0]),
              "drift": DriftTriggered()}[strategy]
    log = [{"day": int(days[0]) - 1, "reason": "initial (days 1-73)", "rows": None,
            "seconds": round(train_seconds, 1)}]
    psi_warnings, parts = [], []
    by_day = {d: g for d, g in stream.groupby("day")}

    for day in days:
        rows = by_day[day]
        s = score(trained, rows)
        part = rows[["day", TARGET]].copy()
        part["score"], part["flagged"] = s, capacity_flags(s)
        parts.append(part)
        if psi(ref_scores, s) > PSI_MAJOR:
            psi_warnings.append(int(day))

        if isinstance(policy, DriftTriggered):
            released = [p for p in parts if p["day"].iloc[0] == day - LABEL_DELAY_DAYS]
            if released:
                frauds = released[0][released[0][TARGET] == 1]
                policy.observe(day, (~frauds["flagged"]).astype(float).to_numpy())

        if policy is not None and policy.wants_retrain(day):
            window = training_rows(df, day)
            trained, secs = train(window, seed)
            ref_scores = score(trained, window)
            reason = "every 30 days" if strategy == "periodic" else "ADWIN on missed fraud"
            log.append({"day": int(day), "reason": reason, "rows": len(window),
                        "seconds": round(secs, 1)})
            if isinstance(policy, DriftTriggered):
                policy.retrained(day)

    alarms = policy.alarms if isinstance(policy, DriftTriggered) else []
    return pd.concat(parts, ignore_index=True), pd.DataFrame(log), psi_warnings, alarms


def weekly(scored):
    first = scored["day"].min()
    scored = scored.assign(week=(scored["day"] - first) // 7 + 1)
    rows = []
    for week, g in scored.groupby("week"):
        y, s = g[TARGET].to_numpy(), g["score"].to_numpy()
        frauds = g[g[TARGET] == 1]
        rows.append({"week": int(week), "PR-AUC": average_precision_score(y, s),
                     "recall @ 1% FPR": recall_at_fpr(y, s),
                     "fraud caught @ 2% reviewed": frauds["flagged"].mean()})
    return pd.DataFrame(rows)


def save_figure(weekly_mean, retrains, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7, 4))
    for name in STRATEGIES:
        w = weekly_mean[weekly_mean["strategy"] == name]
        line, = ax.plot(w["week"], w["PR-AUC"], marker="o", label=name)
        for day in retrains.get(name, []):
            ax.axvline((day - retrains["first_day"]) / 7 + 1, color=line.get_color(),
                       linestyle=":", alpha=0.6)
    ax.set_xlabel("stream week (days 110-182)")
    ax.set_ylabel("weekly PR-AUC (mean of seeds)")
    ax.set_title("E3: static vs periodic vs drift-triggered retraining")
    ax.legend()
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--seeds", type=int, nargs="+", default=SEEDS)
    args = p.parse_args()

    print("loading data and building features...")
    df = load_data()
    df["day"] = df["TransactionDT"] // SECONDS_PER_DAY
    first_day = int(df.loc[df["split"] == "stream", "day"].min())

    summary, weeks, logs, retrain_days = [], [], [], {"first_day": first_day}
    for seed in args.seeds:
        initial = train(df[df["day"].between(*INITIAL_TRAIN_DAYS)], seed)
        for name in STRATEGIES:
            started = time.perf_counter()
            scored, log, warnings, alarms = simulate(df, name, seed, initial)
            w = weekly(scored)
            retrains = len(log) - 1
            summary.append({"strategy": name, "seed": seed,
                            "PR-AUC (whole stream)": average_precision_score(
                                scored[TARGET], scored["score"]),
                            "mean weekly PR-AUC": w["PR-AUC"].mean(),
                            "fraud caught @ 2% reviewed": scored.loc[scored[TARGET] == 1,
                                                                     "flagged"].mean(),
                            "retrains": retrains,
                            "training seconds": log["seconds"].iloc[1:].sum(),
                            "PSI warning days": len(warnings)})
            weeks.append(w.assign(strategy=name, seed=seed))
            logs.append(log.assign(strategy=name, seed=seed))
            if seed == args.seeds[0]:
                retrain_days[name] = log["day"].iloc[1:].tolist()
            print(f"  seed {seed} {name:<8}: PR-AUC {summary[-1]['PR-AUC (whole stream)']:.4f}, "
                  f"caught {summary[-1]['fraud caught @ 2% reviewed']:.3f}, retrains {retrains}"
                  f"{f', ADWIN alarms on days {sorted(set(alarms))}' if alarms else ''} "
                  f"({time.perf_counter() - started:.0f} s)")

    summary = pd.DataFrame(summary)
    table = (summary.groupby("strategy", sort=False)
             .agg(["mean", "std"]).drop(columns="seed", level=0))
    table.columns = [f"{a} ({b})" for a, b in table.columns]
    table = table.round(4).reset_index()
    weeks = pd.concat(weeks, ignore_index=True)
    weekly_mean = weeks.groupby(["strategy", "week"], sort=False).mean(numeric_only=True)
    weekly_mean = weekly_mean.drop(columns="seed").reset_index()
    logs = pd.concat(logs, ignore_index=True)
    print("\n", table.to_string(index=False))

    save_figure(weekly_mean, retrain_days, FIGURES / "e3_weekly.png")
    wide = weekly_mean.pivot(index="week", columns="strategy", values="PR-AUC")[STRATEGIES]
    with open(RESULTS / "e3.md", "w") as f:
        f.write("# E3: static vs periodic vs drift-triggered retraining\n\n")
        f.write(f"Offline simulator over stream days {first_day}-{int(df['day'].max())}. Labels "
                f"arrive {LABEL_DELAY_DAYS} days late; retrains use the last {WINDOW_DAYS} "
                f"labelled days; analysts review the top {REVIEW_CAPACITY:.0%} of each day. All "
                "strategies start from the same model (days 1-73, tuned v1 settings). "
                f"Periodic: every {PERIOD_DAYS} days. Drift: ADWIN on missed fraud, "
                f"{COOLDOWN_DAYS}-day cooldown, at most {WEEKLY_CAP} retrain per week; PSI on "
                f"the score > {PSI_MAJOR} only logs a warning. Seeds {args.seeds}.\n\n")
        f.write("## Summary (mean and std over seeds)\n\n")
        f.write(table.to_markdown(index=False) + "\n\n")
        f.write("![weekly PR-AUC](../figures/e3_weekly.png)\n\n")
        f.write("## Weekly PR-AUC (mean of seeds)\n\n")
        f.write(wide.round(4).to_markdown() + "\n\n")
        f.write("## Every retrain\n\n")
        f.write(logs[logs["day"] >= first_day].to_markdown(index=False) + "\n")
    print("\nsaved docs/results/e3.md and docs/figures/e3_weekly.png")


if __name__ == "__main__":
    main()