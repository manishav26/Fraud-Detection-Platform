"""alert-svc: opens a case for every flagged payment in txn.scored. Design: docs/alerts.md

Reads txn.scored as the consumer group "alert-svc": copies of this service share the partitions,
so each message is handled once. A message's position is saved only after its case is saved, so a
crash means messages are read again, never lost; opening a case twice is harmless.
Analyst routes come in week 3, step 5, commit 4.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import psycopg
from aiokafka import AIOKafkaConsumer
from fastapi import FastAPI

from app.cases import open_case, parse

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
    """Counts since start; a stand-in until the metrics in step 7."""
    return counters
