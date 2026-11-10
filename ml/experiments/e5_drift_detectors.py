"""E5: which drift detector notices which kind of drift, and how fast? ADWIN vs DDM vs PSI.

The real stream already drifts (E3), which would hide when an alarm is "right". So we first
make a calm stream: the stream payments (days 110-182) are shuffled across days, which keeps
every payment but removes any change over time. Then we inject one known drift on day 140
and see who notices:

  control          nothing injected: every alarm is a false alarm
  amounts x3       sudden covariate drift: every amount is 3 times larger from day 140
  missing fields   gradual covariate drift: from day 140 to 160 a growing share of payments
                   (up to 80%) lose their M1-M9 and D1-D15 values, like a broken data feed
  fraud looks      sudden concept drift: from day 140 half of the frauds copy the features of a
  normal           normal payment of the same day; inputs look the same, only the labels say
                   something changed

Detectors (model trained on days 1-73 and never retrained; analysts review the top 2% a day):
  PSI features  daily PSI of every feature vs the first stream week; alarm if any > 0.2
  PSI score     daily PSI of the model score vs the first stream week; alarm if > 0.2
  ADWIN         on missed fraud (1 = a fraud outside the day's top 2%), labels 14 days late
  DDM           same missed-fraud stream as ADWIN
Label-based alarms count on the day the label arrives, when a live system could act.

    python -m ml.experiments.e5_drift_detectors          # 3 seeds, about 5-10 minutes
Writes docs/results/e5.md.
"""

import argparse
import time

import numpy as np
import pandas as pd

from ml.drift.detectors import ADWIN, DDM, EPS, PSI_MAJOR
from ml.experiments.e3_retraining import (
    INITIAL_TRAIN_DAYS,
    LABEL_DELAY_DAYS,
    SEEDS,
    capacity_flags,
    score,
    train,
)
from ml.labels.label_sim import SECONDS_PER_DAY
from ml.train.calibrate import CATEGORICAL, FEATURES, FIGURES, RESULTS, TARGET, load_data

INJECT_AFTER_DAYS = 30        # drift starts 30 days into the stream (day 140)
GRADUAL_DAYS = 20             # missing fields: full strength 20 days later
MAX_MISSING = 0.8
CONCEPT_SHARE = 0.5
REFERENCE_DAYS = 7            # PSI compares each day with the first stream week
SCENARIOS = ["control", "amounts x3", "missing fields", "fraud looks normal"]
DETECTORS = ["PSI features", "PSI score", "ADWIN", "DDM"]
MISSING_COLUMNS = [f"M{i}" for i in range(1, 10)] + [f"D{i}" for i in range(1, 16)]


# ---------- making the stream (tested in tests/test_e5.py) ----------

def calm_stream(stream, rng):
    """Shuffle payments across days: same payments, same daily counts, no change over time."""
    out = stream.copy()
    out["day"] = rng.permutation(out["day"].to_numpy())
    return out.sort_values("day", kind="stable", ignore_index=True)


def inject(stream, scenario, start, rng):
    """Return a copy of the stream with one drift starting on day `start`."""
    out = stream.copy()
    after = out["day"] >= start
    if scenario == "amounts x3":
        out.loc[after, "log_amount"] = np.log1p(np.expm1(out.loc[after, "log_amount"]) * 3)
        if "TransactionAmt" in out:
            out.loc[after, "TransactionAmt"] *= 3
    elif scenario == "missing fields":
        share = np.clip((out["day"] - start + 1) / GRADUAL_DAYS, 0, 1) * MAX_MISSING
        hit = after & (rng.random(len(out)) < share)
        columns = [c for c in MISSING_COLUMNS if c in out]
        out.loc[hit, columns] = np.nan
    elif scenario == "fraud looks normal":
        for _, part in out[after].groupby("day"):
            frauds = part.index[part[TARGET] == 1]
            normals = part.index[part[TARGET] == 0]
            chosen = frauds[rng.random(len(frauds)) < CONCEPT_SHARE]
            if len(chosen) and len(normals):
                donors = rng.choice(normals, len(chosen))
                for f in FEATURES:          # column by column keeps each column's type
                    out.loc[chosen, f] = out.loc[donors, f].to_numpy()
    elif scenario != "control":
        raise ValueError(f"unknown scenario {scenario}")
    return out


# ---------- PSI against a fixed reference (same bins as detectors.psi) ----------

class ReferencePSI:
    """PSI of new windows against one reference window; bins are computed once."""

    def __init__(self, reference, categorical=False, bins=10):
        self.categorical = categorical
        if categorical:
            ref = pd.Series(reference, dtype=object).fillna("<missing>")
            self.ref_share = ref.value_counts(normalize=True)
        else:
            ref = pd.Series(reference, dtype=float)
            edges = np.unique(np.nanquantile(ref, np.linspace(0, 1, bins + 1))) \
                if ref.notna().any() else np.array([0.0])
            if len(edges) < 2:
                edges = np.array([-np.inf, np.inf])
            edges[0], edges[-1] = -np.inf, np.inf
            self.edges = edges
            self.ref_share = self._shares(ref)

    def _shares(self, x):
        counts = np.histogram(x.dropna(), bins=self.edges)[0]
        return np.append(counts, x.isna().sum()) / max(len(x), 1)

    def __call__(self, current):
        if self.categorical:
            cur = pd.Series(current, dtype=object).fillna("<missing>").value_counts(normalize=True)
            values = self.ref_share.index.union(cur.index)
            a = self.ref_share.reindex(values, fill_value=0).to_numpy()
            b = cur.reindex(values, fill_value=0).to_numpy()
        else:
            a, b = self.ref_share, self._shares(pd.Series(current, dtype=float))
        a, b = np.clip(a, EPS, None), np.clip(b, EPS, None)
        return float(np.sum((b - a) * np.log(b / a)))


# ---------- running the detectors ----------

def run_detectors(stream, scores, delay_days=LABEL_DELAY_DAYS):
    """Alarm days of every detector on one scored stream. Returns {detector: [days]} and,
    for PSI features, the worst feature on each alarm day."""
    stream = stream.assign(score=scores)
    days = sorted(stream["day"].unique())
    first = days[0]
    ref = stream[stream["day"] < first + REFERENCE_DAYS]
    feature_psi = {f: ReferencePSI(ref[f], categorical=f in CATEGORICAL) for f in FEATURES}
    score_psi = ReferencePSI(ref["score"])
    alarms = {d: [] for d in DETECTORS}
    worst = {}
    adwin, ddm = ADWIN(), DDM()
    missed_by_day = {}

    for day, part in stream.groupby("day"):
        flagged = capacity_flags(part["score"].to_numpy())
        missed_by_day[day] = (~flagged[part[TARGET].to_numpy() == 1]).astype(float)
        if day >= first + REFERENCE_DAYS:
            values = {f: psi(part[f]) for f, psi in feature_psi.items()}
            name = max(values, key=values.get)
            if values[name] > PSI_MAJOR:
                alarms["PSI features"].append(int(day))
                worst[int(day)] = f"{name} ({values[name]:.2f})"
            if score_psi(part["score"]) > PSI_MAJOR:
                alarms["PSI score"].append(int(day))
        released = missed_by_day.get(day - delay_days)
        if released is not None:
            for m in released:
                if adwin.update(m) and (not alarms["ADWIN"] or alarms["ADWIN"][-1] != day):
                    alarms["ADWIN"].append(int(day))
                if ddm.update(m) and (not alarms["DDM"] or alarms["DDM"][-1] != day):
                    alarms["DDM"].append(int(day))
    return alarms, worst


def outcome(alarm_days, start):
    """First alarm on or after `start` (delay in days) and false alarms before it."""
    after = [d for d in alarm_days if d >= start]
    return {"delay": after[0] - start if after else None,
            "false alarms": sum(1 for d in alarm_days if d < start)}


# ---------- figure ----------

def save_figure(table, false_alarms, path):
    """One grid: drifts (rows) x detectors (columns); each cell says how fast it was caught."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    drifts = list(dict.fromkeys(table["drift"]))
    fig, ax = plt.subplots(figsize=(8, 3.6))
    for i, drift in enumerate([*drifts, "no drift (control)"]):
        for j, detector in enumerate(DETECTORS):
            if i < len(drifts):
                r = table[(table["drift"] == drift) & (table["detector"] == detector)].iloc[0]
                caught = not r["detected"].startswith("0/")
                days = r["mean delay (days)"]
                text = (f"{days:g} day{'' if days == 1 else 's'}\n{r['detected']} runs"
                        if caught else "missed")
                color = "#2e7d32" if caught else "#d9d9d9"
                text_color = "white" if caught else "#555555"
            else:
                fa = false_alarms.set_index("detector").loc[detector].iloc[0]
                text = f"{fa:.2g} false alarm\ndays per run" if fa else "quiet"
                color = "#c62828" if fa else "#e8f5e9"
                text_color = "white" if fa else "#2e7d32"
            ax.add_patch(plt.Rectangle((j, i), 0.96, 0.92, color=color))
            ax.text(j + 0.48, i + 0.46, text, ha="center", va="center", fontsize=10,
                    color=text_color)
    ax.set_xlim(0, len(DETECTORS))
    ax.set_ylim(len(drifts) + 1, 0)
    ax.set_xticks([j + 0.48 for j in range(len(DETECTORS))], DETECTORS)
    ax.set_yticks([i + 0.46 for i in range(len(drifts) + 1)],
                  [*drifts, "no drift (control)"])
    ax.tick_params(length=0)
    ax.xaxis.tick_top()
    for side in ax.spines.values():
        side.set_visible(False)
    ax.set_title("E5: which detector caught which drift, and how many days after it began",
                 pad=28)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)


# ---------- main ----------

def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--seeds", type=int, nargs="+", default=SEEDS)
    args = p.parse_args()

    print("loading data and building features...")
    df = load_data()
    df["day"] = df["TransactionDT"] // SECONDS_PER_DAY
    stream = df[df["split"] == "stream"]
    start = int(stream["day"].min()) + INJECT_AFTER_DAYS
    last = int(stream["day"].max())

    rows, first_psi_feature = [], {}
    for seed in args.seeds:
        rng = np.random.default_rng(seed)
        trained, _ = train(df[df["day"].between(*INITIAL_TRAIN_DAYS)], seed)
        calm = calm_stream(stream, rng)
        for scenario in SCENARIOS:
            started = time.perf_counter()
            drifted = inject(calm, scenario, start, rng)
            alarms, worst = run_detectors(drifted, score(trained, drifted))
            for detector in DETECTORS:
                if scenario == "control":
                    rows.append({"scenario": scenario, "detector": detector, "seed": seed,
                                 "delay": None,
                                 "false alarms": len(alarms[detector])})
                else:
                    rows.append({"scenario": scenario, "detector": detector, "seed": seed,
                                 **outcome(alarms[detector], start)})
            after = [d for d in alarms["PSI features"] if d >= start]
            if scenario != "control" and after and seed == args.seeds[0]:
                first_psi_feature[scenario] = worst[after[0]]
            shown = {k: v[:6] for k, v in alarms.items()}
            print(f"  seed {seed} {scenario:<18}: alarm days {shown} "
                  f"({time.perf_counter() - started:.0f} s)")

    results = pd.DataFrame(rows)
    n = len(args.seeds)
    table = []
    for scenario in SCENARIOS[1:]:
        for detector in DETECTORS:
            r = results[(results["scenario"] == scenario) & (results["detector"] == detector)]
            delays = r["delay"].dropna()
            table.append({"drift": scenario, "detector": detector,
                          "detected": f"{len(delays)}/{n}",
                          "mean delay (days)": round(delays.mean(), 1) if len(delays) else "-",
                          "fastest-slowest": f"{int(delays.min())}-{int(delays.max())}"
                          if len(delays) else "-"})
    table = pd.DataFrame(table)
    control = results[results["scenario"] == "control"]
    false_alarms = (control.groupby("detector", sort=False)["false alarms"]
                    .agg(["mean", "max"]).reindex(DETECTORS).reset_index())
    false_alarms.columns = ["detector", "false alarm days (mean per run)", "worst run"]
    print("\n", table.to_string(index=False))
    print("\ncontrol (no drift injected):\n", false_alarms.to_string(index=False))

    save_figure(table, false_alarms, FIGURES / "e5_detectors.png")
    RESULTS.mkdir(parents=True, exist_ok=True)
    with open(RESULTS / "e5.md", "w") as f:
        f.write("# E5: drift detectors on injected drift (ADWIN vs DDM vs PSI)\n\n")
        f.write(f"Stream days {int(stream['day'].min())}-{last}, shuffled across days so it "
                f"has no drift of its own. One drift injected from day {start}. Model trained "
                "on days 1-73, never retrained; analysts review the top 2% each day. PSI "
                f"compares each day with the first stream week (alarm above {PSI_MAJOR}). "
                f"ADWIN and DDM watch missed fraud; labels arrive {LABEL_DELAY_DAYS} days late, "
                "so their delay can never be below that. Seeds "
                f"{args.seeds}.\n\n")
        f.write("Drifts: **amounts x3** (sudden, inputs change); **missing fields** (gradual "
                f"over {GRADUAL_DAYS} days, up to {MAX_MISSING:.0%} of payments lose M1-M9 and "
                f"D1-D15); **fraud looks normal** (sudden concept drift, "
                f"{CONCEPT_SHARE:.0%} of frauds copy a normal payment's features).\n\n")
        f.write("## Detection delay (days after the drift starts)\n\n")
        f.write(table.to_markdown(index=False) + "\n\n")
        f.write("![detectors](../figures/e5_detectors.png)\n\n")
        if first_psi_feature:
            pointed = "; ".join(f"{k}: {v}" for k, v in first_psi_feature.items())
            f.write(f"Feature PSI's first alarm pointed at: {pointed} "
                    f"(seed {args.seeds[0]}).\n\n")
        f.write("## False alarms (control, no drift injected)\n\n")
        f.write(false_alarms.to_markdown(index=False) + "\n\n")
        f.write(f"Counted as days with an alarm over the {last - int(stream['day'].min()) + 1} "
                "stream days.\n")
    print("\nsaved docs/results/e5.md and docs/figures/e5_detectors.png")


if __name__ == "__main__":
    main()