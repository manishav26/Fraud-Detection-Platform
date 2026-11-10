"""alert-svc: opens a case for every flagged payment in txn.scored. Design: docs/alerts.md

Reads txn.scored as the consumer group "alert-svc": copies of this service share the partitions,
so each message is handled once. A message's position is saved only after its case is saved, so a
crash means messages are read again, never lost; opening a case twice is harmless.

Analysts list, read and resolve cases through Kong (/cases), which checks their token first.
The bank's case-management system reads a case's status and writes back its decision by
transaction id (/v1/cases/txn/...; role case-system).

A second loop sends every audit row to the SIEM (app/siem.py, docs/siem.md).
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Literal

import httpx
import psycopg
from aiokafka import AIOKafkaConsumer
from fastapi import FastAPI, HTTPException, Path, Query, Request
from prometheus_client import Counter, Gauge
from pydantic import BaseModel, Field

from app.cases import (
    DECIDE_ROLES,
    READ_ROLES,
    RESOLVE_ROLES,
    STATUS_ROLES,
    AlreadyResolved,
    get_case,
    get_status_by_txn,
    has_role,
    list_cases,
    open_case,
    parse,
    resolve_by_txn,
    resolve_case,
)
from app.siem import SiemRefused, forward_once, oldest_unsent
from kafka_login import aiokafka_options
from observability.http import instrument

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "localhost:9092")
DSN = os.getenv("ALERTS_DSN", "postgresql://alerts:alerts_dev_only@localhost:5432/fraud")
TOPIC, GROUP = "txn.scored", "alert-svc"
RETRY_SECONDS = 5
log = logging.getLogger("alerts")
# What happened to each txn.scored message: cases_opened, already_open, not_flagged, unreadable
MESSAGES = Counter("alerts_messages_total", "txn.scored messages by outcome", ["outcome"])

# The SIEM (docs/siem.md). No URL: the forwarder is off (unit tests, a laptop without it).
SIEM_URL = os.getenv("SIEM_URL", "")
SIEM_TOKEN = os.getenv("SIEM_TOKEN", "")
SIEM_INTERVAL = float(os.getenv("SIEM_INTERVAL", "1"))   # seconds between rounds when caught up
SIEM_SENT = Counter("alerts_siem_events_sent_total", "Audit rows the SIEM accepted")
SIEM_FAILURES = Counter("alerts_siem_send_failures_total", "Rounds that failed (rows stay unsent)")
SIEM_LAG = Gauge("alerts_siem_lag_seconds",
                 "How long the oldest audit row not yet in the SIEM has waited (0: all sent)")


async def handle(conn, value):
    msg = parse(value)
    if msg is None:
        # Skipping beats retrying: an unreadable message would block every case behind it
        MESSAGES.labels("unreadable").inc()
        log.warning("skipped an unreadable %s message", TOPIC)
        return
    if msg.decision not in ("review", "decline"):
        MESSAGES.labels("not_flagged").inc()
        return
    if await open_case(conn, msg) is None:
        MESSAGES.labels("already_open").inc()
    else:
        MESSAGES.labels("cases_opened").inc()


async def consume():
    """Runs for the life of the service. On any error (Postgres or Redpanda down), start over
    from the last saved position: the unsaved messages are simply read again."""
    while True:
        consumer = AIOKafkaConsumer(TOPIC, bootstrap_servers=KAFKA_BOOTSTRAP, group_id=GROUP,
                                    enable_auto_commit=False, auto_offset_reset="earliest",
                                    **aiokafka_options())   # docs/kafka.md
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


async def forward_to_siem():
    """Runs for the life of the service, apart from the case loop: a SIEM outage never slows
    cases down. Sends until caught up, then checks again every SIEM_INTERVAL seconds."""
    while True:
        try:
            async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn, \
                    httpx.AsyncClient(base_url=SIEM_URL, timeout=5.0) as http:
                log.warning("sending the audit log to the SIEM at %s", SIEM_URL)
                failing = False   # log the first failure and the recovery, not every second
                while True:
                    try:
                        sent = await forward_once(conn, http, SIEM_TOKEN)
                    except psycopg.OperationalError:
                        raise   # the database connection is gone: reconnect below
                    except (httpx.HTTPError, SiemRefused) as e:   # SIEM down, refused, slow
                        SIEM_FAILURES.inc()
                        if not failing:
                            log.warning("SIEM send failed, rows stay unsent until it works: %s", e)
                        failing, sent = True, None
                    else:
                        if failing:
                            log.warning("SIEM sending works again")
                        failing = False
                    if sent:
                        SIEM_SENT.inc(sent)
                    oldest = await oldest_unsent(conn)
                    SIEM_LAG.set(0 if oldest is None else
                                 max(0.0, (datetime.now(UTC) - oldest).total_seconds()))
                    if not sent:   # caught up, failed, or the other copy is sending
                        await asyncio.sleep(SIEM_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("SIEM forwarder stopped, retrying in %d s", RETRY_SECONDS)
            await asyncio.sleep(RETRY_SECONDS)


@asynccontextmanager
async def lifespan(app):
    tasks = [asyncio.create_task(consume())]
    if SIEM_URL:
        tasks.append(asyncio.create_task(forward_to_siem()))
    yield
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="alert-svc", lifespan=lifespan)


instrument(app, "alert-svc")


# ---------- analyst routes, reached through Kong ----------

def connect():
    # One connection per request: analysts send a handful of requests, not hundreds per second
    return psycopg.AsyncConnection.connect(DSN, autocommit=True)


def analyst(request, allowed):
    """Who is acting, and may they? Kong sets both headers after checking the token and the role,
    replacing any a client sends. Checked again here, so a request that somehow skipped Kong's
    rules is still refused (NetworkPolicies, week 4, stop anyone bypassing Kong at all).
    """
    actor = request.headers.get("x-consumer-username")
    if not actor:
        raise HTTPException(403, "case routes must be called through the gateway")
    if not has_role(request.headers.get("x-consumer-groups"), allowed):
        raise HTTPException(403, "your role may not do this")
    return actor


class Resolution(BaseModel):
    outcome: Literal["fraud", "not_fraud"]
    note: str = Field(default="", max_length=500)


@app.get("/cases")
async def cases(request: Request, status: Literal["open", "fraud", "not_fraud"] = "open",
                limit: int = Query(default=50, ge=1, le=200)):
    analyst(request, READ_ROLES)
    async with await connect() as conn:
        return await list_cases(conn, status, limit)


@app.get("/cases/{case_id}")
async def one_case(request: Request, case_id: int):
    analyst(request, READ_ROLES)
    async with await connect() as conn:
        case = await get_case(conn, case_id)
    if case is None:
        raise HTTPException(404, "no such case")
    return case


@app.post("/cases/{case_id}/resolve")
async def resolve(request: Request, case_id: int, body: Resolution):
    actor = analyst(request, RESOLVE_ROLES)
    async with await connect() as conn:
        try:
            case = await resolve_case(conn, case_id, body.outcome, body.note, actor)
        except AlreadyResolved:
            raise HTTPException(409, "case already resolved") from None
    if case is None:
        raise HTTPException(404, "no such case")
    return case



# ---------- the bank's case-management system (week 4, step 7; docs/siem.md) ----------
# It knows payments by their transaction id, not by our case numbers.

TxnId = Path(min_length=1, max_length=128)


class ExternalDecision(BaseModel):
    outcome: Literal["fraud", "not_fraud"]
    note: str = Field(default="", max_length=500)
    external_ref: str = Field(default="", max_length=128)   # their ticket number, for the audit row


@app.get("/v1/cases/txn/{txn_id}")
async def case_status(request: Request, txn_id: str = TxnId):
    analyst(request, STATUS_ROLES)
    async with await connect() as conn:
        case = await get_status_by_txn(conn, txn_id)
    if case is None:
        raise HTTPException(404, "no case for this transaction (approved payments have none)")
    return case


@app.post("/v1/cases/txn/{txn_id}/decision")
async def case_decision(request: Request, body: ExternalDecision, txn_id: str = TxnId):
    actor = analyst(request, DECIDE_ROLES)
    async with await connect() as conn:
        try:
            case = await resolve_by_txn(conn, txn_id, body.outcome, body.note, actor,
                                        body.external_ref)
        except AlreadyResolved:
            raise HTTPException(409, "case already resolved") from None
    if case is None:
        raise HTTPException(404, "no case for this transaction")
    return case
