# ML pipeline

Machine learning and drift detection for the fraud platform (owner: Manisha).

## Setup

Needs Python 3.10+ (we use a conda env with Python 3.11). Download the data first
(see [data/README.md](../data/README.md)).

## Run

From the **repo's top folder**:

```bash
python ml/prep/split_data.py        # time-ordered splits -> data/processed/
python ml/prep/proxy_id.py          # proxy user id check -> docs/results/proxy_id.md
python ml/train/baselines.py        # E1 baselines -> docs/results/e1.md
python ml/train/e2_history.py       # E2: customer history -> docs/results/e2.md
python ml/train/e2_dnorm.py         # E2 extra: normalized D columns -> docs/results/e2_dnorm.md
python -m ml.train.calibrate        # calibration + thresholds -> docs/results/calibration.md
python -m ml.train.train_final      # model v1 -> models/fraud-lgbm-v1/, docs/model_card.md
python -m pytest tests -q           # all tests
```

## What's here

| Path | What it is |
|---|---|
| `ml/prep/` | Data splits by time and the proxy user id check |
| `ml/train/` | Baselines (E1), customer history (E2), calibration, final model training |
| `ml/labels/label_sim.py` | Releases fraud labels 14 simulated days late |
| `ml/model.py` | Packaged model: `FraudModel.load()`, `predict_one()`, `decide()` |
| `features/` (top folder) | Feature code shared by training and live scoring |

## Using the model (scoring service)

```python
from ml.model import FraudModel
model = FraudModel.load("models/fraud-lgbm-v1")
prob = model.predict_one(request, history)   # request: amount, ts, attrs; history: dict or None
decision = model.decide(prob)               # approve / review / decline
```

## Results so far (model v1, test days 92-109)

| Metric | Value |
|---|---|
| PR-AUC | 0.504 (random: 0.035) |
| Fraud caught at 1% false alarms | 42.8% |
| Decision rule | approve < 0.39, review 0.39-0.70, decline >= 0.70 (calibrated) |
| Flagged / precision of flagged | 1.5% of payments / 79% |
| Scoring time | 0.05 ms per transaction (P95) |

Details: [docs/results/](../docs/results/) and [docs/model_card.md](../docs/model_card.md).