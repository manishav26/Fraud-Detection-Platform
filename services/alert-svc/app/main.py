"""alert-svc: opens a case for every flagged payment in txn.scored. Design: docs/alerts.md

Reads txn.scored as the consumer group "alert-svc": copies of this service share the partitions,
so each message is handled once. A message's position is saved only after its case is saved, so a
crash means messages are read again, never lost; opening a case twice is harmless.

Analysts list, read and resolve cases through Kong (/cases), which checks their token first.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Literal

import psycopg
from aiokafka import AIOKafkaConsumer
from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.cases import AlreadyResolved, get_case, list_cases, open_case, parse, resolve_case

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "localhost:9092")
DSN = os.getenv("ALERTS_DSN", "postgresql://alerts:alerts_dev_only@localhost:5432/fraud")
TOPIC, GROUP = "txn.scored", "alert-svc"
RETRY_SECONDS = 5
log = logging.getLogger("alerts")
# Exposed as metrics in week 3, step 7
counters = {"cases_opened": 0, "already_open": 0, "not_flagged": 0, "unreadable": 0}


async def handle(conn, value):
    msg = parse(value)
    if msg is None:
        # Skipping beats retrying: an unreadable message would block every case behind it
        counters["unreadable"] += 1
        log.warning("skipped an unreadable %s message", TOPIC)
        return
    if msg.decision not in ("review", "decline"):
        counters["not_flagged"] += 1
        return
    if await open_case(conn, msg) is None:
        counters["already_open"] += 1
    else:
        counters["cases_opened"] += 1


async def consume():
    """Runs for the life of the service. On any error (Postgres or Redpanda down), start over
    from the last saved position: the unsaved messages are simply read again."""
    while True:
        consumer = AIOKafkaConsumer(TOPIC, bootstrap_servers=KAFKA_BOOTSTRAP, group_id=GROUP,
                                    enable_auto_commit=False, auto_offset_reset="earliest")
        try:
            await consumer.start()
            # autocommit: each case's own transaction (in open_case) decides what is saved
            async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
                log.warning("reading %s as group %s", TOPIC, GROUP)
                while True:
                    batches = await consumer.getmany(timeout_ms=1000)
                    for records in batches.values():
                        for record in records:
                            await handle(conn, record.value)
                    if batches:
                        await consumer.commit()   # only now: every case above is saved
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("stopped reading %s, retrying in %d s", TOPIC, RETRY_SECONDS)
            await asyncio.sleep(RETRY_SECONDS)
        finally:
            await consumer.stop()


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(consume())
    yield
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


app = FastAPI(title="alert-svc", lifespan=lifespan)


@app.get("/stats")
def stats():
    """Counts since start; a stand-in until the metrics in step 7. Not routed through Kong."""
    return counters


# ---------- analyst routes, reached through Kong ----------

def connect():
    # One connection per request: analysts send a handful of requests, not hundreds per second
    return psycopg.AsyncConnection.connect(DSN, autocommit=True)


def analyst(request):
    """Who is acting, as Kong reports it after checking the token. Kong sets this header itself,
    replacing any a client sends. A request without it didn't come through Kong: refused.
    For now every token belongs to one dev client; step 6 gives each analyst their own.
    """
    actor = request.headers.get("x-consumer-username")
    if not actor:
        raise HTTPException(403, "case routes must be called through the gateway")
    return actor


class Resolution(BaseModel):
    outcome: Literal["fraud", "not_fraud"]
    note: str = Field(default="", max_length=500)


@app.get("/cases")
async def cases(request: Request, status: Literal["open", "fraud", "not_fraud"] = "open",
                limit: int = Query(default=50, ge=1, le=200)):
    analyst(request)
    async with await connect() as conn:
        return await list_cases(conn, status, limit)


@app.get("/cases/{case_id}")
async def one_case(request: Request, case_id: int):
    analyst(request)
    async with await connect() as conn:
        case = await get_case(conn, case_id)
    if case is None:
        raise HTTPException(404, "no such case")
    return case


@app.post("/cases/{case_id}/resolve")
async def resolve(request: Request, case_id: int, body: Resolution):
    actor = analyst(request)
    async with await connect() as conn:
        try:
            case = await resolve_case(conn, case_id, body.outcome, body.note, actor)
        except AlreadyResolved:
            raise HTTPException(409, "case already resolved") from None
    if case is None:
        raise HTTPException(404, "no such case")
    return case
