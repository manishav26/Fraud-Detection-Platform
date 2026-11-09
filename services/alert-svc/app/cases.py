"""Opening a case for a flagged payment. Kept apart from the service so it can be tested alone.

A case and its audit row are saved in one transaction: both or neither. txn_id is unique, so a
message delivered twice (at-least-once) opens one case and writes one audit row.
"""

import json

from psycopg.rows import dict_row
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


# ---------- for analysts (routes in main.py) ----------

READ_ROLES = {"analyst", "admin"}   # the same as Kong's rules (deploy/kong/kong.tpl.yml)
RESOLVE_ROLES = {"analyst"}
# The bank's case-management system (week 4, step 7): reads a case's status and writes back the
# decision, by transaction id. Analysts may read the same view.
STATUS_ROLES = {"case-system", "analyst"}
DECIDE_ROLES = {"case-system"}


def has_role(groups_header, allowed):
    """Second check after Kong's: the client's role, from the X-Consumer-Groups header Kong adds."""
    groups = {g.strip() for g in (groups_header or "").split(",") if g.strip()}
    return bool(groups & allowed)


COLUMNS = ("case_id, txn_id, user_id, amount, payment_ts, score, decision, reasons, model_version,"
           " degraded, opened_at, status, resolved_by, resolved_at, note")
OUTCOMES = ("fraud", "not_fraud")
# What the case system sees: enough to work the case, without the customer's id or the amount
STATUS_COLUMNS = ("case_id, txn_id, status, decision, score, reasons, model_version, degraded,"
                  " opened_at, resolved_by, resolved_at")


class AlreadyResolved(Exception):
    """A decision is never silently overwritten: changing one would need its own audited action."""


async def list_cases(conn, status, limit):
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(f"SELECT {COLUMNS} FROM cases WHERE status = %s"
                      " ORDER BY opened_at DESC, case_id DESC LIMIT %s", (status, limit))
    return await cur.fetchall()


async def get_case(conn, case_id):
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(f"SELECT {COLUMNS} FROM cases WHERE case_id = %s", (case_id,))
    return await cur.fetchone()


async def get_status_by_txn(conn, txn_id):
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(f"SELECT {STATUS_COLUMNS} FROM cases WHERE txn_id = %s", (txn_id,))
    return await cur.fetchone()


async def resolve_case(conn, case_id, outcome, note, actor, extra=None):
    """Close an open case. The change and its audit row are saved together, or neither.
    extra: more fields for the audit row (the case system's own ticket number).

    Returns the updated case, or None if there is no such case; raises AlreadyResolved.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}")
    async with conn.transaction():
        cur = conn.cursor(row_factory=dict_row)
        # `status = 'open'` in the same statement: two analysts resolving at once can't both win
        await cur.execute(f"UPDATE cases SET status = %s, resolved_by = %s, resolved_at = now(),"
                          f" note = %s WHERE case_id = %s AND status = 'open' RETURNING {COLUMNS}",
                          (outcome, actor, note, case_id))
        case = await cur.fetchone()
        if case is None:
            if await get_case(conn, case_id) is None:
                return None
            raise AlreadyResolved(case_id)
        await append_async(conn, actor, "case_resolved", f"case:{case_id}", {
            "txn_id": case["txn_id"], "outcome": outcome, "note": note, **(extra or {})})
    return case


async def resolve_by_txn(conn, txn_id, outcome, note, actor, external_ref=""):
    """The case system's write-back: the same as an analyst resolving, found by txn_id.
    Returns the case's status view, or None if no case exists; raises AlreadyResolved."""
    cur = await conn.execute("SELECT case_id FROM cases WHERE txn_id = %s", (txn_id,))
    row = await cur.fetchone()
    if row is None:
        return None
    extra = {"external_ref": external_ref} if external_ref else None
    if await resolve_case(conn, row[0], outcome, note, actor, extra) is None:
        return None
    return await get_status_by_txn(conn, txn_id)
