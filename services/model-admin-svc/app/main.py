"""model-admin-svc: admin-only routes that change the live model, each with an audit row
(week 4, step 6). Design: docs/mlflow.md

The private signing key never comes here. `make promote` signs on the admin's laptop and sends
the signed message; this service only checks it, records who sent it, then publishes it to
model.events, where scoring checks it again. So the route can accept a genuine promotion but can
never create one: a stolen admin token alone can't make any model live.

    GET  /v1/model            what is live, who made it live, and when
    GET  /v1/model/history    every promotion and rollback, newest first
    POST /v1/model/promote    {"token": <signed promotion>}
    POST /v1/model/rollback   {"token": <signed promotion of the previous version>}
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from aiokafka import AIOKafkaProducer
from fastapi import FastAPI, HTTPException, Query, Request
from prometheus_client import Counter
from pydantic import BaseModel, Field

from kafka_login import aiokafka_options
from observability.http import instrument
from registry.signing import MODEL_NAME, TOPIC, InvalidPromotion, verify_promotion

from .rules import check, live_changes
from .store import PostgresStore, Refused

DSN = os.getenv("MODEL_ADMIN_DSN",
                "postgresql://model_admin:model_admin_dev_only@localhost:5432/fraud")
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "localhost:9092")
PROMOTION_KEY = Path(os.getenv("PROMOTION_PUBLIC_KEY_PATH", "promotion-keys/public.pem"))
SEND_TIMEOUT = 10.0   # seconds; aiokafka alone keeps retrying forever (docs/ingest.md)
ADMIN = "admin"       # the same role Kong allows on these routes (deploy/kong/kong.tpl.yml)
log = logging.getLogger("model_admin")
# published, refused (bad message or rule) or failed (Redpanda didn't take it)
CHANGES = Counter("model_changes_total", "Model change requests by outcome", ["action", "outcome"])


async def publish(token):
    # Its Redpanda user is the only one that may write model.events (docs/kafka.md)
    producer = AIOKafkaProducer(bootstrap_servers=KAFKA_BOOTSTRAP, acks="all",
                                enable_idempotence=True, **aiokafka_options())
    await producer.start()
    try:
        # key = model name: compaction keeps the latest message per key (docs/topics.md)
        await asyncio.wait_for(producer.send_and_wait(TOPIC, token.encode(),
                                                      key=MODEL_NAME.encode()), SEND_TIMEOUT)
    finally:
        await producer.stop()


@asynccontextmanager
async def lifespan(app):
    app.state.store = PostgresStore(DSN)
    app.state.public_key = PROMOTION_KEY.read_text() if PROMOTION_KEY.is_file() else None
    app.state.publish = publish
    if app.state.public_key is None:
        log.warning("no promotion key at %s: every promotion will be refused", PROMOTION_KEY)
    yield


app = FastAPI(title="model-admin-svc", lifespan=lifespan)
instrument(app, "model-admin-svc")


def admin(request):
    """Who is acting. Kong checks the token and the role and sets both headers, replacing any a
    client sends; checked again here in case a request reaches the service without Kong."""
    actor = request.headers.get("x-consumer-username")
    groups = {g.strip() for g in (request.headers.get("x-consumer-groups") or "").split(",")}
    if not actor:
        raise HTTPException(403, "model routes must be called through the gateway")
    if ADMIN not in groups:
        raise HTTPException(403, "only an admin may change the model")
    return actor


class SignedPromotion(BaseModel):
    token: str = Field(max_length=8000)


@app.get("/v1/model")
async def live(request: Request):
    admin(request)
    changes = live_changes(await request.app.state.store.history(1000))
    if not changes:
        return {"live": None, "previous": None,
                "note": "no promotion on record: scoring serves the model pinned in "
                        "deploy/scoring.env"}
    last = changes[0]
    # `previous`: the version a rollback would go back to (app/rules.py)
    return {"live": {"version": last["details"]["version"], "label": last["details"]["label"],
                     "sequence": last["details"]["sequence"], "action": last["action"],
                     "by": last["actor"], "at": last["at"],
                     "reason": last["details"].get("reason")},
            "previous": changes[1]["details"]["version"] if len(changes) > 1 else None}


@app.get("/v1/model/history")
async def history(request: Request, limit: int = Query(default=50, ge=1, le=500)):
    admin(request)
    return await request.app.state.store.history(limit)


async def change(request, route, body):
    actor = admin(request)
    state = request.app.state
    if state.public_key is None:
        CHANGES.labels(route, "refused").inc()
        raise HTTPException(503, "no promotion key configured")
    try:
        claims = verify_promotion(body.token, state.public_key)
    except InvalidPromotion as e:
        CHANGES.labels(route, "refused").inc()
        log.warning("%s sent a promotion that is not genuine: %s", actor, e)
        raise HTTPException(400, f"not a genuine promotion: {e}") from None
    try:
        # The audit row is saved before publishing: no model change ever goes out unrecorded
        await state.store.record(actor, route, claims, check)
    except Refused as e:
        CHANGES.labels(route, "refused").inc()
        raise HTTPException(409, str(e)) from None
    try:
        await state.publish(body.token)
    except Exception as e:
        CHANGES.labels(route, "failed").inc()
        log.exception("could not publish %s of version %s", route, claims["version"])
        await state.store.record_failure(actor, claims, f"{type(e).__name__}: {e}")
        raise HTTPException(503, "could not publish to model.events; sign again to retry") from None
    CHANGES.labels(route, "published").inc()
    log.warning("%s: %s made version %s (%s) live, sequence %s", route, actor, claims["version"],
                claims.get("label"), claims["sequence"])
    return {"published": True, "action": route, "version": str(claims["version"]),
            "label": claims.get("label"), "sequence": claims["sequence"], "by": actor}


@app.post("/v1/model/promote")
async def promote(request: Request, body: SignedPromotion):
    return await change(request, "promote", body)


@app.post("/v1/model/rollback")
async def rollback(request: Request, body: SignedPromotion):
    return await change(request, "rollback", body)
