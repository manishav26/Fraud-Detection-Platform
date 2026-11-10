"""ingest-svc: checks each payment, records it in Redpanda, then returns the decision.

Flow per request (design: docs/ingest.md):
  1. FastAPI checks the body against the contract. Bad body -> copy to dlq.txn, return 422.
     A token issued more than 5 minutes ago -> 401 (replays, threat 6; docs/ingest.md).
  2. Replace the user id with its pseudonym (pseudonym.py): nothing after this sees the raw id.
  3. Save the payment to txn.raw (acks=all, idempotent, key user_id). Fails -> 503, client retries.
  4. Ask scoring-svc for score and decision and return it right away (contract: 200 + decision).
     Clients see only the decision (E6 defense D1); the test-only `tester` role sees it all.
  5. In the background, publish payment + decision to txn.scored, where alert-svc opens cases.
"""

import asyncio
import base64
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from aiokafka import AIOKafkaProducer
from aiokafka.errors import KafkaError, KafkaTimeoutError
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from prometheus_client import Counter, Histogram
from pydantic import BaseModel, Field

from features.contract import TEXT_MAX, Attrs, PaymentTime, as_sent, utc_text
from kafka_login import aiokafka_options
from observability.http import BUCKETS, instrument, validation_answer, validation_problems
from pseudonym import pseudonym

SCORING_URL = os.getenv("SCORING_URL", "http://scoring-svc:8000")
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "localhost:9092")
RAW_TOPIC, DLQ_TOPIC, SCORED_TOPIC = "txn.raw", "dlq.txn", "txn.scored"
MAX_DLQ_BODY = 10_000   # characters of a bad body kept in dlq.txn
SEND_TIMEOUT = 5.0      # seconds; then the client gets a 503 instead of a hang
SLOW_MS = 100           # log where the time went for requests slower than this
# Replay window (threat 6, ST12): a captured payment can be sent again only while its token is
# this young. Kong checks the token's signature and expiry (15 min); ingest also checks when it
# was issued (its signed `iat`). Inside the window, txn_id makes a resend harmless.
MAX_TOKEN_AGE = 300     # seconds
CLOCK_SKEW = 30         # seconds of clock difference allowed between the issuer and us
log = logging.getLogger("ingest")
# Where a payment's time goes in ingest: saving to txn.raw, then waiting for scoring
STEP = Histogram("ingest_step_seconds", "Time per ingest step", ["step"], buckets=BUCKETS)
# Payments whose decision never reached txn.scored, so no case can open for them
SCORED_PUBLISH_FAILURES = Counter("ingest_scored_publish_failures_total",
                                  "Decisions that could not be published to txn.scored")
STALE_TOKENS = Counter("ingest_stale_tokens_total",
                       "Payments refused: token issued too long ago, or unreadable")
_background = set()   # running publish tasks; kept here so Python doesn't drop them half-way


def new_producer():
    return AIOKafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP,
        acks="all",                 # wait until every replica has the message
        enable_idempotence=True,    # a resend after a network blip is not stored twice
        value_serializer=lambda v: json.dumps(v).encode(),
        **aiokafka_options(),       # its own Redpanda user on the cluster (docs/kafka.md)
    )


async def publish(producer, topic, value, key=None):
    """Send and wait for Redpanda's confirmation, at most SEND_TIMEOUT seconds.

    aiokafka keeps retrying forever while the broker is down, so we set the limit ourselves.
    """
    try:
        await asyncio.wait_for(producer.send_and_wait(topic, value, key=key), SEND_TIMEOUT)
    except TimeoutError:
        raise KafkaTimeoutError(f"no confirmation from Redpanda in {SEND_TIMEOUT} s") from None


async def start_producer(attempts=30):
    """Redpanda may still be starting when this service starts, so retry for about a minute."""
    for attempt in range(1, attempts + 1):
        producer = new_producer()
        try:
            await producer.start()
            return producer
        except KafkaError:
            await producer.stop()
            if attempt == attempts:
                raise
            log.warning("Redpanda not ready (attempt %d), retrying", attempt)
            await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app):
    app.state.producer = await start_producer()
    # One client for the whole service: it keeps connections to scoring-svc open and reuses
    # them. A new client per request cost a new connection (and setup) every time.
    app.state.http = httpx.AsyncClient(base_url=SCORING_URL, timeout=5.0)
    yield
    await app.state.http.aclose()
    # Let decisions still being published reach txn.scored before the producer closes
    await asyncio.gather(*_background, return_exceptions=True)
    await app.state.producer.stop()


app = FastAPI(title="ingest-svc", lifespan=lifespan)
def _ready():
    # Redpanda producer started and the scoring client made (both at startup)
    if getattr(app.state, "producer", None) is None or getattr(app.state, "http", None) is None:
        return False, "still starting"
    return True, ""


instrument(app, "ingest-svc", ready=_ready)


# Ids are letters, digits and . _ : | - only (ST6, docs/ingest.md): an id with a quote, a newline
# or a bracket is refused here (422, copy to dlq.txn) instead of travelling on to logs, Cassandra
# and cases. Free text (channel, text attrs) stays free; every store takes it as a parameter.
ID_PATTERN = r"^[A-Za-z0-9._:|-]+$"


class Transaction(BaseModel):
    txn_id: str = Field(min_length=1, max_length=128, pattern=ID_PATTERN)
    user_id: str = Field(min_length=1, max_length=128, pattern=ID_PATTERN)
    amount: float = Field(gt=0)
    # A time zone offset of a real zone (-12:00 to +14:00), years 1970-2099; stored in UTC
    ts: PaymentTime
    channel: str | None = Field(default=None, max_length=TEXT_MAX)   # the device type, as text
    # The 51 attrs, each typed as features/schema.yaml says (features/contract.py): text in a
    # number attr, an unknown attr or text over 256 characters gets 422 here
    attrs: Attrs = Field(default_factory=Attrs)


class Decision(BaseModel):
    txn_id: str
    score: float
    decision: str
    model_version: str
    reasons: list[str] = []  # up to 3 model reasons, plus probe detection's; empty when approved
    cold_start: bool = False  # true: no trusted history for this user (unknown or new)
    degraded: bool = False    # true: scored without history because Cassandra didn't answer
    probe: bool = False       # true: probe detection (D3) turned an approval into a review


class ShortDecision(BaseModel):
    """What clients get (E6 defense D1, docs/scoring.md): the score and reasons would tell
    someone probing the API which change brings a flagged payment closer to approval."""
    txn_id: str
    decision: str


# Roles that get the full answer: only the test-only load-test client, so replay and load tests
# can report scores, cold starts and degraded answers. Analysts see it all in cases. Anything
# else, including a missing or unknown role, gets the short answer: a Kong mistake can't leak it.
FULL_ANSWER_ROLES = {"tester"}


def answer_for(groups_header, decision):
    """The decision as the caller may see it, from the role Kong passes (X-Consumer-Groups).
    Kong sets that header itself and replaces one the client sent (acl plugin)."""
    groups = {g.strip() for g in (groups_header or "").split(",") if g.strip()}
    if groups & FULL_ANSWER_ROLES:
        return decision
    return ShortDecision(txn_id=decision.txn_id, decision=decision.decision)


def now():
    return datetime.now(UTC).isoformat()


def token_age(authorization, at=None):
    """Seconds since the caller's token was issued, from its `iat` claim; None if there is no
    readable token. The signature is not checked here: Kong has checked it before any request
    reaches us, and only Kong can reach us (network rules and mTLS, docs/network.md)."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    parts = authorization.split(" ", 1)[1].strip().split(".")
    if len(parts) != 3:
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        return (at if at is not None else time.time()) - float(claims["iat"])
    except (ValueError, KeyError, TypeError):
        return None


def check_token_age(authorization):
    age = token_age(authorization)
    if age is None or age > MAX_TOKEN_AGE + CLOCK_SKEW or age < -CLOCK_SKEW:
        STALE_TOKENS.inc()
        raise HTTPException(401, "token too old for a payment (more than 5 minutes): get a new one")


# A reused connection to a scoring pod that is shutting down can break mid-request (the pod
# closed it). Trying once more opens a fresh connection to a live pod. Safe: scoring the same
# payment again gives the same answer and writes the same history row. Timeouts are not retried:
# that would only double a slow wait.
BROKEN_CONNECTION = (httpx.ConnectError, httpx.ReadError, httpx.WriteError,
                     httpx.RemoteProtocolError)
SCORING_RETRIES = Counter("ingest_scoring_retries_total",
                          "Scoring calls tried again after a broken connection")


async def call_scoring(record):
    try:
        r = await app.state.http.post("/score", json=record)
    except BROKEN_CONNECTION as e:
        SCORING_RETRIES.inc()
        log.warning("scoring connection broke for %s (%s); trying once more",
                    record["txn_id"], type(e).__name__)
        r = await app.state.http.post("/score", json=record)
    r.raise_for_status()
    return r.json()


# Scoring failed after the payment was recorded in txn.raw, by kind: scoring answered with an
# error status, didn't answer in time, the connection broke (after the one retry), or its answer
# wasn't a decision. Each one is a bug or an outage to look at, not a client mistake.
SCORING_FAILURES = Counter("ingest_scoring_failures_total",
                           "Payments recorded but not scored, answered 503", ["kind"])


def failure_kind(error):
    if isinstance(error, httpx.HTTPStatusError):
        return "error_answer"
    if isinstance(error, httpx.TimeoutException):
        return "timeout"
    if isinstance(error, httpx.HTTPError):
        return "connection"
    return "bad_answer"   # not JSON, or not a decision (ValueError, pydantic's ValidationError)


async def scored_or_503(record):
    """scoring-svc's decision, or 503 if there is none. The payment is already safe in txn.raw and
    txn_id makes a retry harmless, so the client is told exactly that (the contract's 503), not a
    bare 500. Nothing goes to txn.scored: the retry does that."""
    try:
        return Decision(**await call_scoring(record))
    except (httpx.HTTPError, ValueError) as e:
        kind = failure_kind(e)
        SCORING_FAILURES.labels(kind).inc()
        log.error("recorded %s but could not score it (%s: %s); client told to retry",
                  record["txn_id"], kind, e)
        raise HTTPException(503, "recorded but not scored: retry with the same txn_id") from None


async def publish_scored(producer, record, decision):
    """Payment + decision to txn.scored. Runs after the client has its answer: the decision is
    made and the payment is already safe in txn.raw, so a failure here must not become an error.
    The cost is that a flagged payment could miss its case, so every failure is logged and counted.
    """
    try:
        await publish(producer, SCORED_TOPIC, {**record, **decision, "scored_at": now()},
                      key=record["user_id"].encode())
    except KafkaError:
        SCORED_PUBLISH_FAILURES.inc()
        log.warning("could not publish %s to %s (decision %s): no case will open for it",
                    record["txn_id"], SCORED_TOPIC, decision.get("decision"))


def in_background(coroutine):
    task = asyncio.create_task(coroutine)
    _background.add(task)
    task.add_done_callback(_background.discard)


def scrub(body):
    """A rejected body with its user id pseudonymized, when it is a JSON object with a text
    user_id: the dead-letter queue is the one place raw input is kept. Anything else is kept as
    sent (it may not even be JSON); docs/privacy.md lists that as a limit."""
    try:
        data = json.loads(body)
    except ValueError:
        return body
    if isinstance(data, dict) and isinstance(data.get("user_id"), str):
        return json.dumps({**data, "user_id": pseudonym(data["user_id"])})
    return body


@app.exception_handler(RequestValidationError)
async def send_to_dlq(request: Request, exc: RequestValidationError):
    """Keep a copy of every rejected body in dlq.txn, then answer 422 like every service does:
    where each problem is and what it is, never the value sent (observability/http.py)."""
    body = scrub((await request.body()).decode(errors="replace"))[:MAX_DLQ_BODY]
    errors = validation_problems(exc)
    try:
        await publish(request.app.state.producer, DLQ_TOPIC,
                      {"path": request.url.path, "errors": errors, "body": body,
                       "received_at": now()})
    except KafkaError:
        log.exception("could not write rejected request to %s", DLQ_TOPIC)
    return validation_answer(exc)


# No response_model: answer_for picks the shape per caller (a union would let FastAPI choose)
@app.post("/v1/transactions/score", response_model=None)
async def score_txn(txn: Transaction, request: Request):
    check_token_age(request.headers.get("authorization"))   # before anything is stored
    record = txn.model_dump(mode="json")
    record["attrs"] = as_sent(txn.attrs)   # the attrs the client sent, no added nulls
    record["ts"] = utc_text(txn.ts)          # one form for every reader: UTC, "...Z"
    # From here on the payment carries only the pseudonym: txn.raw, scoring, Cassandra, txn.scored,
    # cases and the SIEM never see the raw id (threat 8, docs/privacy.md)
    record["user_id"] = pseudonym(record["user_id"])
    start = time.perf_counter()
    try:
        # key = user_id: one user's payments go to one partition, so they stay in order
        await publish(request.app.state.producer, RAW_TOPIC, record,
                      key=record["user_id"].encode())
    except KafkaError:
        log.exception("could not save %s to %s", txn.txn_id, RAW_TOPIC)
        raise HTTPException(503, "could not record the transaction; retry with the same txn_id")
    published = time.perf_counter()

    # scoring-svc decides (model.decide, thresholds saved with the model): one source of truth
    decision = await scored_or_503(record)
    done = time.perf_counter()
    STEP.labels("publish_raw").observe(published - start)
    STEP.labels("wait_scoring").observe(done - published)
    if (done - start) * 1000 >= SLOW_MS:
        log.warning("slow %s: publish %.0f ms, scoring %.0f ms", txn.txn_id,
                    (published - start) * 1000, (done - published) * 1000)
    # txn.scored keeps everything: alert-svc and the SIEM need it
    in_background(publish_scored(request.app.state.producer, record, decision.model_dump()))
    return answer_for(request.headers.get("x-consumer-groups"), decision).model_dump()
