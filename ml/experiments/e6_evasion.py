"""E6: can a fraudster probe the API until a flagged payment gets approved? And do our
defenses stop it without bothering real customers? (threat 14)

The attacker holds a fraud payment that the live model flags (review or decline). They resend
it with small changes to the fields they control (amount between 50% and 100% of the
original, hour, e-mail domains, device type, card brand and type) and watch the answer. The
attack succeeds if one variant is approved within Q queries. Q = 10, 50 and 200.

  no defense    the API returns the score: the attacker keeps any change that lowers it
  D1            hide the score: clients only see approve / review / decline, so the
                attacker can only keep changes that improve the decision (decline -> review)
  D1+D2         plus a query budget: at most 20 payments per card per day
  D1+D2+D3      plus probe detection: after 5 flagged payments on one card within 24 hours,
                every later payment on that card goes to review

Real customers pay a price for D2 and D3, so we replay the legitimate payments of days
92-182 and count how many extra would be blocked or sent to review (false positives).
Plan's pass rule: a clear drop in success at Q = 50, false positives up by at most 0.1 point.

    python -m ml.experiments.e6_evasion --model models/fraud-lgbm-v1-tuned     # ~5-10 min
Writes docs/results/e6.md and docs/figures/e6_evasion.png.
"""

import argparse
import math

import numpy as np
import pandas as pd

from ml.eval.promotion_gate import golden_set
from ml.experiments.e3_retraining import SEEDS
from ml.model import FraudModel
from ml.train.calibrate import FIGURES, RESULTS, TARGET, load_data

CONTROLLABLE = ["log_amount", "hour", "P_emaildomain", "R_emaildomain", "DeviceType",
                "card4", "card6"]
MIN_AMOUNT_SHARE = 0.5        # the attacker still wants at least half of the money
QUERIES = [10, 50, 200]
N_ATTACKS = 200               # flagged frauds attacked per seed
BUDGET = 20                   # D2: payments per card per day
PROBE_FLAGS = 5               # D3: flagged payments per card within PROBE_WINDOW
PROBE_WINDOW = 86_400         # seconds
LEVEL = {"approve": 0, "review": 1, "decline": 2}
DEFENSES = ["no defense", "D1", "D1+D2", "D1+D3", "D1+D2+D3"]


# ---------- the attacker (tested in tests/test_e6.py) ----------

def value_pools(rows, fields):
    """Values the attacker may pick for each categorical field: what real customers send."""
    return {f: np.array([r.get(f) for r in rows], dtype=object) for f in fields}


def mutate(row, original, rng, fields, pools):
    """Change one controllable field of `row` (the amount stays 50-100% of `original`'s)."""
    new = dict(row)
    f = fields[rng.integers(len(fields))]
    if f == "log_amount":
        amount = math.expm1(original["log_amount"]) * rng.uniform(MIN_AMOUNT_SHARE, 1)
        new[f] = math.log1p(amount)
    elif f == "hour":
        new[f] = int(rng.integers(24))
    else:
        new[f] = pools[f][rng.integers(len(pools[f]))]
    return new


def attack(model, originals, rng, pools, max_queries, feedback):
    """Hill-climb every original at once. Returns, per attack, the query number of the first
    approval (or inf). feedback="score": keep a change that lowers the score; "decision":
    keep a change only if the decision gets better (decline -> review)."""
    fields = [f for f in CONTROLLABLE if f in model.features]
    best = [dict(r) for r in originals]
    probs = model.predict_rows(best)
    best_value = probs if feedback == "score" else np.array(
        [LEVEL[model.decide(p)] for p in probs], dtype=float)
    first = np.full(len(originals), np.inf)
    for q in range(1, max_queries + 1):
        active = np.flatnonzero(np.isinf(first))
        if not len(active):
            break
        tries = [mutate(best[i], originals[i], rng, fields, pools) for i in active]
        p = model.predict_rows(tries)
        for i, row, prob in zip(active, tries, p):
            decision = model.decide(prob)
            if decision == "approve":
                first[i] = q
                continue
            value = prob if feedback == "score" else LEVEL[decision]
            if value < best_value[i]:
                best[i], best_value[i] = row, value
    return first


def success(first, queries, defense, budget=BUDGET, probe_flags=PROBE_FLAGS):
    """Share of attacks approved within `queries` under one defense setting.

    `first` holds the first-approval query numbers of the matching attacker (score feedback
    for "no defense", decision feedback for the others). Every query before the first
    approval was flagged, so with D3 the card is frozen after `probe_flags` of them."""
    limit = queries
    if "D2" in defense:
        limit = min(limit, budget)
    if "D3" in defense:
        limit = min(limit, probe_flags)
    return float(np.mean(first <= limit))


# ---------- the cost for real customers ----------

def encode(model, df):
    """Feature matrix the way FraudModel encodes it, but vectorised (fast on 200k rows)."""
    columns = []
    for f in model.features:
        s = df[f]
        if f in model.categories:
            cats = model.categories[f]
            columns.append(s.map(lambda v, c=cats: c.get(str(v), np.nan)
                                 if not (v is None or (isinstance(v, float) and math.isnan(v)))
                                 else np.nan).astype(float).to_numpy())
        else:
            columns.append(pd.to_numeric(s, errors="coerce").astype(float).to_numpy())
    return np.column_stack(columns)


def over_budget(uids, days, budget=BUDGET):
    """True for a payment beyond the card's `budget` payments of that day (input in time order)."""
    rank = pd.DataFrame({"u": uids, "d": days}).groupby(["u", "d"]).cumcount().to_numpy()
    return rank >= budget


def probe_frozen(uids, times, flagged, probe_flags=PROBE_FLAGS, window=PROBE_WINDOW):
    """True for a payment whose card already had `probe_flags` flagged payments in the last
    `window` seconds (input in time order)."""
    frozen = np.zeros(len(uids), dtype=bool)
    frame = pd.DataFrame({"u": uids, "t": times, "f": flagged.astype(int)})
    for _, g in frame.groupby("u", sort=False):
        if g["f"].sum() < probe_flags:
            continue
        t, f = g["t"].to_numpy(), g["f"].to_numpy()
        before = np.concatenate([[0], np.cumsum(f)[:-1]])        # flagged before each one
        left = np.searchsorted(t, t - window, side="left")
        in_window = before - np.concatenate([[0], np.cumsum(f)])[left]
        frozen[g.index.to_numpy()] = in_window >= probe_flags
    return frozen


def customer_cost(model, df):
    """Share of legitimate payments not approved, without and with D2 and D3."""
    df = df.sort_values("TransactionDT", kind="stable", ignore_index=True)
    probs = model.calibrate(model.booster.predict(encode(model, df)))
    flagged = probs >= model.meta["thresholds"]["review_threshold"]
    known = df["uid_known"].to_numpy(dtype=bool)
    uids = np.where(known, df["uid"].astype(str), "unknown-" + df.index.astype(str))
    days = (df["TransactionDT"] // 86_400).to_numpy()
    blocked = over_budget(uids, days) & known
    frozen = probe_frozen(uids, df["TransactionDT"].to_numpy(), flagged) & known
    legit = df[TARGET].to_numpy() == 0
    base = flagged[legit].mean()
    with_d2 = (flagged | blocked)[legit].mean()
    with_d3 = (flagged | blocked | frozen)[legit].mean()
    return {"legit payments": int(legit.sum()), "flagged by the model": base,
            "plus D2 (budget)": with_d2, "plus D3 (probe detection)": with_d3,
            "D3 only (no budget)": (flagged | frozen)[legit].mean()}


# ---------- main ----------

def save_figure(curves, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7, 4))
    for name, values in curves.items():
        ax.plot(range(1, len(values) + 1), np.asarray(values) * 100, label=name)
    for q in QUERIES:
        ax.axvline(q, color="grey", linestyle=":", alpha=0.5)
    ax.set_xlabel("queries the attacker may send (Q)")
    ax.set_ylabel("flagged frauds turned into approvals (%)")
    ax.set_xscale("log")
    ax.set_title("E6: evasion by probing, with and without defenses")
    ax.legend()
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--model", default="models/fraud-lgbm-v1-tuned")
    p.add_argument("--seeds", type=int, nargs="+", default=SEEDS)
    args = p.parse_args()

    model = FraudModel.load(args.model)
    print("loading data and building features...")
    df = load_data()
    golden = golden_set(df)
    rows = golden[model.features].to_dict("records")
    probs = model.predict_rows(rows)
    is_fraud = golden[TARGET].to_numpy() == 1
    flagged_frauds = [r for r, p, y in zip(rows, probs, is_fraud)
                      if y and model.decide(p) != "approve"]
    pools = value_pools([r for r, y in zip(rows, is_fraud) if not y], CONTROLLABLE)
    print(f"golden set days 92-109: {len(flagged_frauds):,} frauds flagged by the model")

    q_max = max(QUERIES)
    firsts = {"score": [], "decision": []}
    for seed in args.seeds:
        rng = np.random.default_rng(seed)
        picked = rng.choice(len(flagged_frauds), min(N_ATTACKS, len(flagged_frauds)),
                            replace=False)
        originals = [flagged_frauds[i] for i in picked]
        for feedback, results in firsts.items():
            first = attack(model, originals, rng, pools, q_max, feedback)
            results.append(first)
            print(f"  seed {seed} attacker sees {feedback:<8}: approved within {q_max} "
                  f"queries {np.mean(np.isfinite(first)):.1%}")

    table, curves = [], {}
    for defense in DEFENSES:
        feedback = "score" if defense == "no defense" else "decision"
        row = {"defense": defense}
        for q in QUERIES:
            per_seed = [success(f, q, defense) for f in firsts[feedback]]
            row[f"Q={q}"] = f"{np.mean(per_seed):.1%} ± {np.std(per_seed):.1%}"
        table.append(row)
        all_first = np.concatenate(firsts[feedback])
        curves[defense] = [success(all_first, q, defense) for q in range(1, q_max + 1)]
    table = pd.DataFrame(table)
    print("\n", table.to_string(index=False))

    print("\nmeasuring the cost for real customers (days 92-182)...")
    later = df[df["TransactionDT"] // 86_400 >= 92]
    cost = customer_cost(model, later)
    base = cost["flagged by the model"]
    extra = {"D1+D2+D3": (cost["plus D3 (probe detection)"] - base) * 100,
             "D1+D3": (cost["D3 only (no budget)"] - base) * 100}
    cost_table = pd.DataFrame([
        {"setting": "model only (and D1: hiding the score changes no decision)",
         "legit payments not approved": f"{base:.3%}"},
        {"setting": f"+ D2 budget ({BUDGET} per card per day)",
         "legit payments not approved": f"{cost['plus D2 (budget)']:.3%}"},
        {"setting": f"+ D2 + D3 probe detection ({PROBE_FLAGS} flagged per card in 24 h)",
         "legit payments not approved": f"{cost['plus D3 (probe detection)']:.3%}"},
        {"setting": "+ D3 only (no budget)",
         "legit payments not approved": f"{cost['D3 only (no budget)']:.3%}"},
    ])
    print("\n", cost_table.to_string(index=False))

    q50 = {d: np.mean([success(f, 50, d) for f in firsts["score" if d == "no defense"
                                                           else "decision"]])
           for d in DEFENSES}
    verdicts = []
    for combo in ["D1+D2+D3", "D1+D3"]:
        passed = q50[combo] < q50["no defense"] / 2 and extra[combo] <= 0.1
        verdicts.append(f"{combo}: {'PASS' if passed else 'FAIL'}: at Q=50 success falls from "
                        f"{q50['no defense']:.1%} to {q50[combo]:.1%}; false positives up by "
                        f"{extra[combo]:.3f} points (limit 0.1).")
    print("\n" + "\n".join(verdicts))

    save_figure(curves, FIGURES / "e6_evasion.png")
    RESULTS.mkdir(parents=True, exist_ok=True)
    with open(RESULTS / "e6.md", "w") as f:
        f.write("# E6: evasion by probing the API (threat 14)\n\n")
        f.write(f"Model `{args.model}`. {min(N_ATTACKS, len(flagged_frauds))} frauds per seed "
                "that the model flags on the golden days 92-109; the attacker changes only "
                "fields they control (amount 50-100% of the original, hour, e-mail domains, "
                "device, card brand and type) and succeeds if a variant is approved. Seeds "
                f"{args.seeds}. Simulated offline against the real model; D1-D3 are the "
                "platform changes this would justify.\n\n")
        f.writelines(f"**{v}**\n\n" for v in verdicts)
        f.write("D1+D3 (no budget) was added after the first run, which showed that the "
                "budget (D2) caused almost all of the false positives while adding no "
                "protection once probe detection (D3) is on.\n\n")
        f.write("## Share of flagged frauds turned into approvals\n\n")
        f.write(table.to_markdown(index=False) + "\n\n")
        f.write("![evasion](../figures/e6_evasion.png)\n\n")
        f.write("D1 hides the score (clients see only the decision); D2 allows "
                f"{BUDGET} payments per card per day; D3 sends every later payment on a card "
                f"to review after {PROBE_FLAGS} flagged ones within 24 hours.\n\n")
        f.write("## Cost for real customers (legitimate payments, days 92-182)\n\n")
        f.write(cost_table.to_markdown(index=False) + "\n\n")
        f.write("Cards are the proxy user id (card1 + addr1 + first-seen day); payments "
                "whose card cannot be identified are not limited, which is also a gap an "
                "attacker could use.\n")
    print("saved docs/results/e6.md and docs/figures/e6_evasion.png")


if __name__ == "__main__":
    main()