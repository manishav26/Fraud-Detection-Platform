# ML pipeline

Machine learning, drift detection and ML experiments for the fraud platform (owner: Manisha).

## Setup

Needs Python 3.11+ (we use a conda env `fraud` with Python 3.11). Download the data first
(see [data/README.md](../data/README.md)).

## Run

From the **repo's top folder**:

```bash
# data and baselines
python ml/prep/split_data.py               # time-ordered splits -> data/processed/
python ml/prep/proxy_id.py                 # proxy user id check -> docs/results/proxy_id.md
python ml/train/baselines.py               # E1 baselines -> docs/results/e1.md
python -m ml.train.e1_seeds                # E1 with 3 seeds -> docs/results/e1_seeds.md (~20-30 min)
python ml/train/e2_history.py              # E2: customer history -> docs/results/e2.md
python ml/train/e2_dnorm.py                # E2 extra: normalized D columns -> docs/results/e2_dnorm.md

# model v1
python -m ml.train.tune                    # random search -> ml/configs/lgbm.yaml, docs/results/tuning.md
python -m ml.train.calibrate               # calibration + thresholds -> docs/results/calibration.md
python -m ml.train.train_final             # model v1 -> models/fraud-lgbm-v1/, docs/model_card.md
python -m ml.train.reasons_report          # TreeSHAP reasons + speed -> docs/results/reasons.md

# checks and experiments
python -m ml.eval.parity --mode stream     # train/serve parity (needs the stack) -> docs/results/parity_stream.md
python -m ml.drift.drift_report            # drift on the stream days -> docs/results/drift.md
python -m ml.experiments.e3_retraining     # E3 retraining strategies -> docs/results/e3.md
python -m ml.experiments.e5_drift_detectors  # E5 injected drift -> docs/results/e5.md
python -m ml.experiments.e6_evasion --model models/fraud-lgbm-v1   # E6 evasion -> docs/results/e6.md
python -m ml.eval.promotion_gate --candidate <new model folder> --current models/fraud-lgbm-v1

python -m pytest tests -q                  # all tests
```

**Careful:** `train_final` writes into `models/fraud-lgbm-v1/`, the folder scoring loads. A
rebuild on a different CPU gives slightly different files that won't match the pins in
`deploy/scoring.env`, so back up the pinned folder first.

## What's here

| Path | What it is |
|---|---|
| `ml/prep/` | Data splits by time and the proxy user id check |
| `ml/train/` | Baselines (E1, E2), tuning, calibration, final model, reasons report |
| `ml/configs/lgbm.yaml` | Tuned LightGBM settings (written by `tune.py`) |
| `ml/model.py` | Packaged model: `FraudModel.load()`, `predict_one()`, `explain_one()`, `decide()` |
| `ml/labels/label_sim.py` | Releases fraud labels 14 simulated days late |
| `ml/drift/` | Detectors (PSI, KS, ADWIN, DDM) and the stream drift report |
| `ml/eval/` | Train/serve parity test and the promotion gate |
| `ml/experiments/` | E3 (retraining), E5 (injected drift), E6 (evasion and defenses) |
| `features/` (top folder) | Feature code shared by training and live scoring |

## Using the model (scoring service)

```python
from ml.model import FraudModel
model = FraudModel.load("models/fraud-lgbm-v1")
prob = model.predict_one(request, history)   # request: amount, ts, attrs; history: dict or None
decision = model.decide(prob)               # approve / review / decline
reasons = model.explain_one(request, history)  # top 3 reasons (TreeSHAP)
```

## Results (model v1, test days 92-109)

| Metric | Value |
|---|---|
| PR-AUC | 0.498 (random: 0.035) |
| Fraud caught at 1% false alarms | 43.4% |
| Decision rule | approve < 0.55, review 0.55-0.70, decline >= 0.70 (calibrated; lowest threshold within the 2% review budget) |
| Flagged / precision of flagged | 1.3% of payments / 83% |
| Scoring time | 0.15 ms per transaction (P95); reasons 2.9 ms |

Numbers are for the deployed build `fraud-lgbm-v1-535eb73c`. My experiments used an equivalent
build (`26eff82a`, same settings, PR-AUC 0.497); different CPUs give slightly different files.

## Experiments in short

| | Result |
|---|---|
| Parity | Live history features equal the training ones on 10,000 of 10,000 payments (stream and full mode) |
| E3 retraining | Retraining recovers +0.04 PR-AUC lost to drift; drift-triggered ties periodic but retrains 4 vs 2 times |
| E5 drift detectors | Feature PSI catches input drift in 0-1 days; only ADWIN on missed fraud catches concept drift (18 days, incl. the 14-day label delay) |
| E6 evasion | Hiding the score + probe detection cut attack success at Q=50 from 50% to 17.5% offline, +0.001 points false positives. Live it's about 55% (see [e6_live.md](../docs/results/e6_live.md)) |
| Promotion gate | Tuned v1 vs the week-2 model: +0.001 PR-AUC, a tie; v1 kept for reproducibility and fewer flags |

Details: [docs/results/](../docs/results/) and [docs/model_card.md](../docs/model_card.md).