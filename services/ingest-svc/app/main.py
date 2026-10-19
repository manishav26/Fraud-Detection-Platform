import os
from datetime import datetime

import httpx
from fastapi import FastAPI
from pydantic import BaseModel, Field

SCORING_URL = os.getenv("SCORING_URL", "http://scoring-svc:8000")
app = FastAPI(title="ingest-svc (stub)")


class Transaction(BaseModel):
    txn_id: str
    user_id: str
    amount: float = Field(gt=0)
    ts: datetime
    channel: str | None = None
    attrs: dict[str, float | str | None] = Field(default={}, max_length=64)


class Decision(BaseModel):
    txn_id: str
    score: float
    decision: str
    model_version: str


def decide(score: float, review: float = 0.30, block: float = 0.70) -> str:
    # Thresholds mirror config/decision.yaml; loaded from it in week 2.
    return "block" if score >= block else "review" if score >= review else "allow"


@app.post("/v1/transactions/score", response_model=Decision)
async def score_txn(txn: Transaction):
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{SCORING_URL}/score", json=txn.model_dump(mode="json"))
    body = r.json()
    return Decision(txn_id=txn.txn_id, score=body["score"],
                    decision=decide(body["score"]), model_version=body["model_version"])
