"""ingest-svc: checks each payment, records it in Redpanda, then returns the decision.

Flow per request (design: docs/ingest.md):
  1. FastAPI checks the body against the contract. Bad body -> copy to dlq.txn, return 422.
  2. Save the payment to txn.raw (acks=all, idempotent, key user_id). Fails -> 503, client retries.
  3. Ask scoring-svc for score and decision and return it right away (contract: 200 + decision).
"""

import asyncio
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
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from pydantic import AwareDatetime, BaseModel, Field

SCORING_URL = os.getenv("SCORING_URL", "http://scoring-svc:8000")
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "localhost:9092")
RAW_TOPIC, DLQ_TOPIC = "txn.raw", "dlq.txn"
MAX_DLQ_BODY = 10_000   # characters of a bad body kept in dlq.txn
SEND_TIMEOUT = 5.0      # seconds; then the client gets a 503 instead of a hang
SLOW_MS = 100           # log where the time went for requests slower than this
log = logging.getLogger("ingest")


def new_producer():
    return AIOKafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP,
        acks="all",                 # wait until every replica has the message
        enable_idempotence=True,    # a resend after a network blip is not stored twice
        value_serializer=lambda v: json.dumps(v).encode(),
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
    await app.state.producer.stop()


app = FastAPI(title="ingest-svc", lifespan=lifespan)


class Transaction(BaseModel):
    txn_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    amount: float = Field(gt=0)
    ts: AwareDatetime   # must carry a timezone, e.g. 2026-10-19T10:00:00Z
    channel: str | None = None
    attrs: dict[str, float | str | None] = Field(default={}, max_length=64)


class Decision(BaseModel):
    txn_id: str
    score: float
    decision: str
    model_version: str
    reasons: list[str] = []  # up to 3 model reasons; empty when approved
    cold_start: bool = False  # true: no trusted history for this user (unknown or new)
    degraded: bool = False    # true: scored without history because Cassandra didn't answer


def now():
    return datetime.now(UTC).isoformat()


async def call_scoring(record):
    r = await app.state.http.post("/score", json=record)
    return r.json()


@app.exception_handler(RequestValidationError)
async def send_to_dlq(request: Request, exc: RequestValidationError):
    """Keep a copy of every rejected body in dlq.txn, then answer 422 as FastAPI normally does."""
    body = (await request.body()).decode(errors="replace")[:MAX_DLQ_BODY]
    errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
    try:
        await publish(request.app.state.producer, DLQ_TOPIC,
                      {"path": request.url.path, "errors": errors, "body": body,
                       "received_at": now()})
    except KafkaError:
        log.exception("could not write rejected request to %s", DLQ_TOPIC)
    return await request_validation_exception_handler(request, exc)


@app.post("/v1/transactions/score", response_model=Decision)
async def score_txn(txn: Transaction, request: Request):
    record = txn.model_dump(mode="json")
    start = time.perf_counter()
    try:
        # key = user_id: one user's payments go to one partition, so they stay in order
        await publish(request.app.state.producer, RAW_TOPIC, record, key=txn.user_id.encode())
    except KafkaError:
        log.exception("could not save %s to %s", txn.txn_id, RAW_TOPIC)
        raise HTTPException(503, "could not record the transaction; retry with the same txn_id")
    published = time.perf_counter()

    # scoring-svc decides (model.decide, thresholds saved with the model): one source of truth
    body = await call_scoring(record)
    done = time.perf_counter()
    if (done - start) * 1000 >= SLOW_MS:
        log.warning("slow %s: publish %.0f ms, scoring %.0f ms", txn.txn_id,
                    (published - start) * 1000, (done - published) * 1000)
    return Decision(**body)
