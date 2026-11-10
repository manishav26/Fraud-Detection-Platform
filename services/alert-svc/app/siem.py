"""Sends the audit log to the SIEM (week 4, step 7). Design: docs/siem.md

The audit log is the outbox: every security event (cases opened and resolved, models promoted,
rolled back or refused) is already a row there, in order, saved in the same transaction as what
it records. This loop sends the rows the SIEM hasn't had yet, oldest first, and moves its saved
position (table siem_cursor) only after the SIEM has accepted them:

- SIEM down: nothing is lost; the rows wait in Postgres and go out when it answers again.
  Cases keep opening meanwhile: this runs apart from the case path.
- At least once: a crash between "SIEM accepted" and "position saved" sends a batch again. Each
  event carries its audit row id, and the SIEM drops ids it already has.
- Two alert-svc copies: the position row is locked while a batch is sent (FOR UPDATE SKIP LOCKED),
  so one copy sends and the other skips that round.
- No gaps: audit rows are written one at a time under the chain's lock (audit/chain.py), so ids
  become visible in order and `id > last sent` can't skip a row still being written.

Kept apart from main.py so it can be tested alone.
"""

import json

from audit.chain import canonical_time

BATCH = 500   # rows per request; a normal second has a handful
SOURCE, SOURCETYPE = "fraud-platform", "fraud:audit"
COLUMNS = "id, at, actor, action, target, details, prev_hash, hash"
LOCK_CURSOR = "SELECT last_id FROM siem_cursor WHERE id = 1 FOR UPDATE SKIP LOCKED"
UNSENT = f"SELECT {COLUMNS} FROM audit_log WHERE id > %s ORDER BY id LIMIT %s"
SAVE = "UPDATE siem_cursor SET last_id = %s, sent_at = now() WHERE id = 1"
# How long the oldest unsent row has waited (0 when all are sent). Read without the lock, so
# every copy can report it.
OLDEST_UNSENT = ("SELECT a.at FROM audit_log a, siem_cursor c WHERE c.id = 1 AND a.id > c.last_id"
                 " ORDER BY a.id LIMIT 1")


class SiemRefused(Exception):
    """The SIEM answered, but not with success: the batch stays unsent and is tried again."""


def to_event(row):
    """An audit row -> one HEC event. `time` is when the row was written, so the SIEM orders
    events by when they happened and can show the delay."""
    row_id, at, actor, action, target, details, prev_hash, digest = row
    return {"time": at.timestamp(), "source": SOURCE, "sourcetype": SOURCETYPE,
            "event": {"id": row_id, "at": canonical_time(at), "actor": actor, "action": action,
                      "target": target, "details": details, "prev_hash": prev_hash,
                      "hash": digest}}


def batch_body(rows):
    """HEC batch mode: one JSON object per line."""
    return "\n".join(json.dumps(to_event(r), sort_keys=True) for r in rows)


async def forward_once(conn, http, token, batch=BATCH):
    """Send up to `batch` unsent rows. Returns how many were sent, or None if another copy is
    sending right now. Raises on any failure, leaving the position where it was."""
    async with conn.transaction():
        cur = await conn.execute(LOCK_CURSOR)
        cursor = await cur.fetchone()
        if cursor is None:
            return None   # locked by the other copy (or the table isn't set up: see db-schema)
        cur = await conn.execute(UNSENT, (cursor[0], batch))
        rows = await cur.fetchall()
        if not rows:
            return 0
        r = await http.post("/services/collector/event", content=batch_body(rows),
                            headers={"Authorization": f"Splunk {token}"})
        if r.status_code != 200:
            raise SiemRefused(f"SIEM answered {r.status_code}: {r.text[:200]}")
        await conn.execute(SAVE, (rows[-1][0],))
    return len(rows)


async def oldest_unsent(conn):
    """When the oldest row not yet sent was written, or None if everything is sent."""
    cur = await conn.execute(OLDEST_UNSENT)
    row = await cur.fetchone()
    return row[0] if row else None
