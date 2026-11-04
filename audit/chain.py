"""The hash-chained audit log (table audit_log, deploy/postgres/platform.sql). Design: docs/alerts.md

Each row stores hash = SHA-256(previous row's hash + this row's fields). Change, remove or reorder
any row, and every link after it stops matching, which `make verify-audit` reports (test ST8).
"""

import hashlib
import json
from datetime import UTC, datetime

GENESIS = "0" * 64   # the "previous hash" of the first row
# Any constant works: it makes appends wait for each other, so two rows can't claim the same
# previous row. An advisory lock, because locking the table itself needs UPDATE rights, which the
# append-only login must not have.
CHAIN_LOCK = 427_001


def canonical_time(at):
    """The same text every time, whether `at` comes from Python or back from Postgres."""
    return at.astimezone(UTC).isoformat(timespec="microseconds")


def row_hash(prev_hash, at, actor, action, target, details):
    fields = {"prev_hash": prev_hash, "at": canonical_time(at), "actor": actor, "action": action,
              "target": target, "details": details}
    text = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


LOCK = "SELECT pg_advisory_xact_lock(%s)"   # held until the transaction ends
LAST = "SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1"
INSERT = ("INSERT INTO audit_log (at, actor, action, target, details, prev_hash, hash)"
          " VALUES (%s, %s, %s, %s, %s, %s, %s)")


def _new_row(last, actor, action, target, details):
    prev_hash = last[0] if last else GENESIS
    at = datetime.now(UTC)   # microseconds, the same precision Postgres keeps
    digest = row_hash(prev_hash, at, actor, action, target, details)
    return digest, (at, actor, action, target, json.dumps(details), prev_hash, digest)


def append(conn, actor, action, target, details):
    """Add one row, inside the caller's transaction, so it is saved together with what it
    records (for example the case it opened), or not at all. Returns the new row's hash."""
    with conn.cursor() as cur:
        cur.execute(LOCK, (CHAIN_LOCK,))
        cur.execute(LAST)
        digest, values = _new_row(cur.fetchone(), actor, action, target, details)
        cur.execute(INSERT, values)
    return digest


async def append_async(conn, actor, action, target, details):
    """The same, for an async connection (alert-svc)."""
    async with conn.cursor() as cur:
        await cur.execute(LOCK, (CHAIN_LOCK,))
        await cur.execute(LAST)
        digest, values = _new_row(await cur.fetchone(), actor, action, target, details)
        await cur.execute(INSERT, values)
    return digest


def first_broken(rows, start=GENESIS):
    """rows: (id, at, actor, action, target, details, prev_hash, hash) in id order.
    Returns (id, reason) for the first row that breaks the chain, or None if it is intact.
    start: the hash the first row should follow; GENESIS when checking from the very beginning."""
    expected_prev = start
    for row_id, at, actor, action, target, details, prev_hash, digest in rows:
        if prev_hash != expected_prev:
            return row_id, "does not follow the row before it (removed, added or reordered rows)"
        if row_hash(prev_hash, at, actor, action, target, details) != digest:
            return row_id, "its contents were changed after it was written"
        expected_prev = digest
    return None
