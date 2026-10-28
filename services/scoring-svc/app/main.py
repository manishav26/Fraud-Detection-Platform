"""scoring-svc: fraud score and decision for one payment (week 2, step 5). Design: docs/scoring.md

Per request:
  1. Known user? Read their earlier payments from Cassandra and build the 12 history features
     (features/history.py, the same code as training). Unknown or new user -> cold start.
  2. Manisha's model gives a calibrated fraud probability; model.decide() turns it into
     approve / review / decline with the thresholds saved inside the model (config/decision.yaml).
  3. Save the payment to Cassandra, so the user's next payment sees it.

At startup the model files must match the SHA-256 values pinned in the deploy config
(deploy/scoring.env), not only manifest.json in the model folder (threat 11, test ST9).
"""

import asyncio
import hashlib
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI
from pydantic import BaseModel

from features.history import NOVELTY_OF, history_for_one
from ml.model import FILES, FraudModel
from store.history import HistoryStore, to_datetime

MODEL_DIR = Path(os.getenv("MODEL_DIR", "models/fraud-lgbm-v1"))
DECISION_CONFIG = Path(os.getenv("DECISION_CONFIG", "config/decision.yaml"))
PIN_ENV = {"model.txt": "MODEL_SHA256_MODEL_TXT", "meta.json": "MODEL_SHA256_META_JSON"}
UNKNOWN_PART = "NA"   # features/proxy_id.py writes this for a missing part of the proxy id
log = logging.getLogger("scoring")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_pins(folder, pins):
    """Refuse model files that differ from the pinned fingerprints.

    The pins come from the deploy config, outside the model folder, so an attacker who replaces
    the model files and rewrites manifest.json to match is still caught.
    """
    for f in FILES:
        if not pins.get(f):
            raise RuntimeError(f"no pinned SHA-256 for {f}: set {PIN_ENV[f]}")
        if sha256(Path(folder) / f) != pins[f]:
            raise RuntimeError(f"{f} does not match its pinned SHA-256; refusing to load")


class StubModel:
    """Only when no model folder exists (fresh clone, CI). Fixed score, real thresholds."""

    def __init__(self, thresholds):
        self.meta = {"thresholds": thresholds}

    def predict_one(self, request, history=None):
        return 0.12

    decide = FraudModel.decide   # same rule as the real model


def load_model():
    """(model, version). Missing model -> stub. Present but not matching the pins -> error."""
    if not (MODEL_DIR / "model.txt").exists():
        log.warning("no model in %s: using the stub (score 0.12)", MODEL_DIR)
        return StubModel(yaml.safe_load(DECISION_CONFIG.read_text())), "stub-0"
    verify_pins(MODEL_DIR, {f: os.getenv(env) for f, env in PIN_ENV.items()})
    model = FraudModel.load(MODEL_DIR)
    return model, model.meta["info"]["name"]


def is_known(user_id):
    """Training gives no history to ids with a missing part (uid_known = False); so do we."""
    return UNKNOWN_PART not in user_id.split("|")


def history_for(store, req):
    """(history features, cold_start). None = no trusted history: the model sees missing values."""
    if not is_known(req.user_id):
        return None, True
    try:
        past = store.past(req.user_id, req.ts)
    except Exception:   # Cassandra down: score without history rather than fail the payment
        log.exception("history read failed for %s; scoring as cold start", req.txn_id)
        return None, True
    values = {c: req.attrs.get(c) for c in NOVELTY_OF}
    features = history_for_one(past, to_datetime(req.ts).timestamp(), req.amount, values)
    return features, not past


async def connect_store(attempts=30):
    """Cassandra takes about a minute to start, so retry; then fail and let Docker restart us."""
    for attempt in range(1, attempts + 1):
        try:
            return HistoryStore()
        except Exception:
            if attempt == attempts:
                raise
            log.warning("Cassandra not ready (attempt %d), retrying", attempt)
            await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app):
    app.state.model, app.state.version = load_model()
    app.state.store = await connect_store()
    yield
    app.state.store.close()


app = FastAPI(title="scoring-svc", lifespan=lifespan)


class ScoreRequest(BaseModel):
    txn_id: str
    user_id: str
    amount: float
    ts: str
    channel: str | None = None
    attrs: dict[str, float | str | None] = {}


@app.post("/score")
def score(req: ScoreRequest):
    # Plain `def`: FastAPI runs it in a worker thread, so the blocking Cassandra and model calls
    # don't stall other requests.
    store = app.state.store
    history, cold_start = history_for(store, req)
    prob = app.state.model.predict_one(req.model_dump(), history)
    if is_known(req.user_id):
        try:
            store.save(req.model_dump())   # before answering: the user's next payment needs it
        except Exception:
            log.exception("history write failed for %s", req.txn_id)
    return {"txn_id": req.txn_id, "score": prob, "decision": app.state.model.decide(prob),
            "model_version": app.state.version, "reasons": [], "cold_start": cold_start}
