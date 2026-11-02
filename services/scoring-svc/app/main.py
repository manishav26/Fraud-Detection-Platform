"""scoring-svc: fraud score and decision for one payment (week 2, step 5). Design: docs/scoring.md

Per request:
  1. Known user? Read their earlier payments from Cassandra and build the 12 history features
     (features/history.py, the same code as training). Unknown or new user -> cold start.
  2. Manisha's model gives a calibrated fraud probability; model.decide() turns it into
     approve / review / decline with the thresholds saved inside the model (config/decision.yaml).
  3. Review or decline: add the top 3 reasons (TreeSHAP, model.explain_one).
  4. Save the payment to Cassandra, so the user's next payment sees it.

At startup the model files must match the SHA-256 values pinned in the deploy config
(deploy/scoring.env), not only manifest.json in the model folder (threat 11, test ST9). After that,
a signed promotion message can replace the model without a restart (registry/live_model.py).
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
from registry.live_model import LiveModel, follow_promotions
from store.history import HistoryStore, to_datetime

MODEL_DIR = Path(os.getenv("MODEL_DIR", "models/fraud-lgbm-v1"))
DECISION_CONFIG = Path(os.getenv("DECISION_CONFIG", "config/decision.yaml"))
PIN_ENV = {"model.txt": "MODEL_SHA256_MODEL_TXT", "meta.json": "MODEL_SHA256_META_JSON"}
PROMOTION_KEY = Path(os.getenv("PROMOTION_PUBLIC_KEY_PATH", "promotion-keys/public.pem"))
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "localhost:9092")
CATCH_UP_SECONDS = 30   # at most this long at startup to learn which model is live
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

    def explain_one(self, request, history=None):
        return []

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


def reasons_for(model, decision, request, history):
    """Top reasons for flagged payments only: about 98% are approved, which saves 3 ms each.

    A reason is an extra: if explaining fails, the payment still gets its decision.
    """
    if decision == "approve":
        return []
    try:
        return model.explain_one(request, history)
    except Exception:
        log.exception("could not explain %s", request["txn_id"])
        return []


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
    model, version = load_model()
    app.state.store = await connect_store()
    follower = None
    if PROMOTION_KEY.is_file():
        app.state.live = LiveModel(model, version, PROMOTION_KEY.read_text(), FraudModel.load)
        caught_up = asyncio.Event()
        follower = asyncio.create_task(follow_promotions(app.state.live, KAFKA_BOOTSTRAP,
                                                         caught_up))
        try:
            # A restarted pod should open its port already serving the promoted model
            await asyncio.wait_for(caught_up.wait(), CATCH_UP_SECONDS)
        except TimeoutError:
            log.warning("promotions not read yet: serving %s and still trying", version)
    else:
        log.warning("no promotion key at %s: promotions are off, serving %s", PROMOTION_KEY, version)
        app.state.live = LiveModel(model, version, None, FraudModel.load)
    yield
    if follower:
        follower.cancel()
        await asyncio.gather(follower, return_exceptions=True)
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
    # One copy for the whole request: a swap during it can't mix two models or names
    model, version = app.state.live.current
    store, request = app.state.store, req.model_dump()
    history, cold_start = history_for(store, req)
    prob = model.predict_one(request, history)
    decision = model.decide(prob)
    reasons = reasons_for(model, decision, request, history)
    if is_known(req.user_id):
        try:
            store.save(request)   # before answering: the user's next payment needs it
        except Exception:
            log.exception("history write failed for %s", req.txn_id)
    return {"txn_id": req.txn_id, "score": prob, "decision": decision,
            "model_version": version, "reasons": reasons, "cold_start": cold_start}
