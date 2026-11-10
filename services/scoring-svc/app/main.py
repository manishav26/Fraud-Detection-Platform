"""scoring-svc: fraud score and decision for one payment (week 2, step 5). Design: docs/scoring.md

Per request:
  1. Known user? Read their earlier payments from Cassandra and build the 12 history features
     (features/history.py, the same code as training). Unknown or new user -> cold start.
  2. Manisha's model gives a calibrated fraud probability; model.decide() turns it into
     approve / review / decline with the thresholds saved inside the model (config/decision.yaml).
  3. Probe detection (D3, store/probe.py): an approval on a card with 5 or more payments the
     model flagged in the last 24 h becomes a review.
  4. Review or decline: add the top 3 reasons (TreeSHAP, model.explain_one).
  5. Save the payment, and whether the model flagged it, to Cassandra: the user's next payment
     sees it.

At startup the model files must match the SHA-256 values pinned in the deploy config
(deploy/scoring.env), not only manifest.json in the model folder (threat 11, test ST9). After that,
a signed promotion message can replace the model without a restart (registry/live_model.py).
"""

import asyncio
import hashlib
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI
from prometheus_client import Counter, Histogram, Info
from pydantic import BaseModel, Field

from features.contract import TEXT_MAX, Attrs, PaymentTime, as_sent, utc_text
from features.history import NOVELTY_OF, history_for_one
from ml.model import FILES, FraudModel
from observability.http import BUCKETS, instrument
from registry.live_model import LiveModel, follow_promotions
from registry.signing import model_label
from store.guarded import GuardedStore, HistoryUnavailable
from store.history import HistoryStore, is_known, to_datetime
from store.probe import REASON as PROBE_REASON
from store.probe import hold

MODEL_DIR = Path(os.getenv("MODEL_DIR", "models/fraud-lgbm-v1"))
DECISION_CONFIG = Path(os.getenv("DECISION_CONFIG", "config/decision.yaml"))
PIN_ENV = {"model.txt": "MODEL_SHA256_MODEL_TXT", "meta.json": "MODEL_SHA256_META_JSON"}
PROMOTION_KEY = Path(os.getenv("PROMOTION_PUBLIC_KEY_PATH", "promotion-keys/public.pem"))
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "localhost:9092")
CATCH_UP_SECONDS = 30   # at most this long at startup to learn which model is live
SLOW_MS = 100           # log where the time went for requests slower than this
# D3 on or off (deploy/scoring.env). Off only to show the attack working (make k8s-probe-attack)
PROBE_DETECTION = os.getenv("PROBE_DETECTION", "on") != "off"
log = logging.getLogger("scoring")

# Where each payment's time goes, to find the slow step during slow periods (docs/metrics.md)
STEP = Histogram("scoring_step_seconds", "Time per scoring step", ["step"], buckets=BUCKETS)
DECISIONS = Counter("scoring_decisions_total", "Payments scored",
                    ["decision", "cold_start", "degraded"])
MODEL = Info("scoring_model", "The model answering right now")
PROBE_HOLDS = Counter("scoring_probe_holds_total",
                      "Approvals sent to review by probe detection (D3)")


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
    pins = {f: os.getenv(env) for f, env in PIN_ENV.items()}
    verify_pins(MODEL_DIR, pins)
    model = FraudModel.load(MODEL_DIR)
    return model, model_label(MODEL_DIR, pins["model.txt"])   # same label as if promoted


def history_for(store, req):
    """(history features, past, cold_start, degraded). None = no trusted history: the model sees
    missing values, and probe detection can't run. cold_start: the user has no history.
    degraded: we couldn't get it.
    """
    if not is_known(req.user_id):
        return None, None, True, False
    try:
        past = store.past(req.user_id, req.ts)
    except HistoryUnavailable:   # score without history rather than fail or delay the payment
        return None, None, False, True
    values = {c: getattr(req.attrs, c) for c in NOVELTY_OF}
    features = history_for_one(past, to_datetime(req.ts).timestamp(), req.amount, values)
    return features, past, not past, False


def probe_check(decision, past, ts):
    """(final decision, held). Needs the card's history: unknown cards and degraded mode skip it."""
    if not PROBE_DETECTION or past is None:
        return decision, False
    return hold(decision, past, to_datetime(ts).timestamp())


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
    app.state.store = GuardedStore(await connect_store())
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


def _model_now():
    # Read at each scrape, so the dashboard shows a promotion as soon as it happens
    live = getattr(app.state, "live", None)
    if live is not None:
        MODEL.info({"version": live.current[1]})


def _ready():
    # Model loaded and Cassandra connected (both happen at startup). A Cassandra outage later
    # does not make scoring unready: it still answers, degraded, and that beats an error.
    if getattr(app.state, "live", None) is None:
        return False, "model not loaded yet"
    if getattr(app.state, "store", None) is None:
        return False, "Cassandra not connected yet"
    return True, ""


instrument(app, "scoring-svc", before_scrape=_model_now, ready=_ready)


class ScoreRequest(BaseModel):
    txn_id: str
    user_id: str
    amount: float
    ts: PaymentTime   # the same rule as ingest (features/contract.py)
    channel: str | None = Field(default=None, max_length=TEXT_MAX)
    # The same strict contract as ingest (features/contract.py): scoring never trusts input that
    # skipped ingest's check, and the model only ever sees numbers where it expects numbers
    attrs: Attrs = Field(default_factory=Attrs)


@app.post("/score")
def score(req: ScoreRequest):
    # Plain `def`: FastAPI runs it in a worker thread, so the blocking Cassandra and model calls
    # don't stall other requests.
    # One copy for the whole request: a swap during it can't mix two models or names
    model, version = app.state.live.current
    store, request = app.state.store, req.model_dump()
    request["attrs"] = as_sent(req.attrs)   # as ingest sent them
    request["ts"] = utc_text(req.ts)
    start = time.perf_counter()
    history, past, cold_start, degraded = history_for(store, req)
    read = time.perf_counter()
    prob = model.predict_one(request, history)
    flagged = model.decide(prob)   # the model's own decision: only this counts toward D3
    decision, probe = probe_check(flagged, past, req.ts)
    predicted = time.perf_counter()
    reasons = reasons_for(model, decision, request, history)
    if probe:
        PROBE_HOLDS.inc()
        reasons = [PROBE_REASON, *reasons]
    explained = time.perf_counter()
    if is_known(req.user_id):
        try:
            # before answering: the user's next payment needs it
            store.save({**request, "flagged": flagged != "approve"})
        except HistoryUnavailable:
            pass   # counted by the store; the payment is still safe in txn.raw
    done = time.perf_counter()
    known = is_known(req.user_id)   # unknown users skip Cassandra: their 0 ms would hide slow reads
    for step, seconds, applies in (("history_read", read - start, known),
                                   ("predict", predicted - read, True),
                                   ("reasons", explained - predicted, decision != "approve"),
                                   ("history_write", done - explained, known)):
        if applies:
            STEP.labels(step).observe(seconds)
    DECISIONS.labels(decision, str(cold_start).lower(), str(degraded).lower()).inc()
    if (done - start) * 1000 >= SLOW_MS:
        log.warning("slow %s: read %.0f ms, predict %.0f ms, reasons %.0f ms, write %.0f ms",
                    req.txn_id, (read - start) * 1000, (predicted - read) * 1000,
                    (explained - predicted) * 1000, (done - explained) * 1000)
    return {"txn_id": req.txn_id, "score": prob, "decision": decision,
            "model_version": version, "reasons": reasons, "cold_start": cold_start,
            "degraded": degraded, "probe": probe}
