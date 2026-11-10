"""The audit log's model rows, read and written with model-admin-svc's own login, which may only
read and add audit rows (deploy/postgres/platform.sql)."""

import psycopg
from psycopg.rows import dict_row

from audit.chain import CHAIN_LOCK, LOCK, append_async

from .rules import ACTIONS, FAILED, MODEL_ACTIONS, details

HISTORY = ("SELECT id, at, actor, action, target, details FROM audit_log"
           " WHERE action = ANY(%s) ORDER BY id DESC LIMIT %s")


class Refused(Exception):
    """The promotion may not go out; the message says why."""


class PostgresStore:
    def __init__(self, dsn):
        self.dsn = dsn

    def _connect(self):
        # One connection per call: admins send a few requests a day, not hundreds per second
        return psycopg.AsyncConnection.connect(self.dsn, autocommit=True)

    async def history(self, limit=50):
        async with await self._connect() as conn:
            return await _history(conn, limit)

    async def record(self, actor, route, claims, check):
        """Check and record in one transaction, holding the audit chain's lock, so two promotions
        sent at once can't both pass the sequence check. Raises Refused."""
        async with await self._connect() as conn, conn.transaction():
            await conn.execute(LOCK, (CHAIN_LOCK,))
            reason = check(route, claims, await _history(conn, 1000))
            if reason:
                raise Refused(reason)
            await append_async(conn, actor, ACTIONS[route], f"model:{claims['version']}",
                               details(claims))

    async def record_failure(self, actor, claims, error):
        async with await self._connect() as conn, conn.transaction():
            await append_async(conn, actor, FAILED, f"model:{claims['version']}",
                               {"sequence": claims["sequence"], "error": error})


async def _history(conn, limit):
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(HISTORY, (list(MODEL_ACTIONS), limit))
    return await cur.fetchall()
