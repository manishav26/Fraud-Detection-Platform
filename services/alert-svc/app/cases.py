"""Opening a case for a flagged payment. Kept apart from the service so it can be tested alone.

A case and its audit row are saved in one transaction: both or neither. txn_id is unique, so a
message delivered twice (at-least-once) opens one case and writes one audit row.
"""

import json

from pydantic import AwareDatetime, BaseModel, ValidationError

from audit.chain import append_async

FLAGGED = {"review", "decline"}   # scoring's decision, so thresholds live in one place
ACTOR = "alert-svc"

INSERT_CASE = """
INSERT INTO cases (txn_id, user_id, amount, payment_ts, score, decision, reasons, model_version,
                   degraded)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (txn_id) DO NOTHING
RETURNING case_id
"""


class Scored(BaseModel):
    """The fields of a txn.scored message that a case needs (docs/topics.md)."""
    txn_id: str
    user_id: str
    amount: float
    ts: AwareDatetime
    score: float
    decision: str
    model_version: str
    reasons: list[str] = []
    degraded: bool = False


def parse(value):
    """Message bytes -> Scored, or None if it can't be read (logged and skipped by the caller)."""
    try:
        return Scored.model_validate(json.loads(value))
    except (ValueError, ValidationError):   # bad JSON is a ValueError too
        return None


async def open_case(conn, msg):
    """Returns the new case id, or None: not flagged, or a case already exists for it."""
    if msg.decision not in FLAGGED:
        return None
    async with conn.transaction():
        cur = await conn.execute(INSERT_CASE, (
            msg.txn_id, msg.user_id, msg.amount, msg.ts, msg.score, msg.decision,
            json.dumps(msg.reasons), msg.model_version, msg.degraded))
        row = await cur.fetchone()
        if row is None:
            return None   # delivered again: the case and its audit row are already there
        case_id = row[0]
        await append_async(conn, ACTOR, "case_opened", f"case:{case_id}", {
            "txn_id": msg.txn_id, "decision": msg.decision, "score": msg.score,
            "model_version": msg.model_version, "degraded": msg.degraded})
    return case_id
