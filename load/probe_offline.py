"""Why the live probe attack beats D3 more often than E6 predicted (docs/results/e6_live.md).

    python -m load.probe_offline --seed 1794254637      # about 5 minutes, no cluster needed

The live attack (load/probe_attack.py, same attacker, same seed), against the real model on the
laptop instead of the API. Each variant is scored two ways:
  history grows   the card's history holds the attacker's own earlier tries, as on the platform:
                  after the first try, its e-mail and device are no longer new to the card
  history frozen  the history stays as it was before the attack, as E6 assumed
Each card starts with an empty history (its real earlier payments are left out), so the
difference is the attacker's own tries. "Beats D3": the first approval comes before the card
has 5 flags, i.e. at query 4 or earlier (the payment itself is the first flag).
"""

import argparse

import numpy as np

from features.history import NOVELTY_OF, history_for_one
from load import probe_attack as pa
from load.replay import attr_columns, load_rows, to_request
from ml.model import FraudModel
from store.history import is_known, to_datetime
from store.probe import PROBE_FLAGS


def offline_send(model, grows):
    past = []

    def send(req):
        t = to_datetime(req["ts"]).timestamp()
        values = {c: req["attrs"].get(c) for c in NOVELTY_OF}
        history = history_for_one(past if grows else [], t, req["amount"], values)
        if grows:
            past.append({"t": t, "amount": req["amount"], **values})
        return model.decide(model.predict_one(req, history))
    return send


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--model", default="models/fraud-lgbm-v1")
    p.add_argument("--data", default="data/processed/stream.parquet")
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--cards", type=int, default=100)
    args = p.parse_args()
    model = FraudModel.load(args.model)
    df = load_rows(args.data)
    frauds = df[(df["isFraud"] == 1) & df["uid"].map(is_known)]
    attrs, pools = attr_columns(), pa.value_pools(df)

    print("| History | Flagged cards | Approved within 30 tries | Beats D3 |")
    print("|---|---|---|---|")
    for grows in (False, True):
        rng = np.random.default_rng(args.seed)
        rows = []
        for i in rng.permutation(len(frauds)):
            if len(rows) == args.cards:
                break
            row = pa.attack_card(offline_send(model, grows), to_request(frauds.iloc[i].to_dict(),
                                 attrs), rng, pools, 30, "offline")
            if row:
                rows.append(row)
        first = [r["first_approval"] for r in rows]
        approved = sum(q is not None for q in first)
        beats = sum(q is not None and q < PROBE_FLAGS for q in first)
        print(f"| {'grows (as live)' if grows else 'frozen (as E6)'} | {len(rows)} | "
              f"{approved / len(rows):.0%} | {beats / len(rows):.0%} |")


if __name__ == "__main__":
    main()
