"""alert-svc opening cases. Postgres tests run in a transaction that is always rolled back."""

import asyncio
import importlib.util
import json
import uuid
from pathlib import Path

import psycopg
import pytest

from audit.chain import first_broken

ROOT = Path(__file__).resolve().parents[1]
# Several services have a package called `app`; load alert-svc's module by path
_spec = importlib.util.spec_from_file_location("alert_cases",
                                               ROOT / "services/alert-svc/app/cases.py")
cases = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cases)

ALERTS_DSN = "postgresql://alerts:alerts_dev_only@localhost:5432/fraud"


def message(decision="decline", **change):
    return json.dumps({"txn_id": f"pytest-{uuid.uuid4()}", "user_id": "u1", "amount": 120.0,
                       "ts": "2017-12-10T10:00:00Z", "score": 0.91, "decision": decision,
                       "model_version": "fraud-lgbm-v1", "reasons": ["C1=72 (+2.32)"],
                       "degraded": False, "cold_start": False, **change}).encode()


def test_reads_a_scored_message():
    msg = cases.parse(message())
    assert msg.decision == "decline" and msg.reasons == ["C1=72 (+2.32)"]


@pytest.mark.parametrize("bad", [b"not json", b"[]", message(ts="2017-12-10T10:00:00"),
                                 json.dumps({"txn_id": "t1"}).encode()])
def test_unreadable_message_is_none(bad):
    assert cases.parse(bad) is None


def test_approved_payment_opens_nothing():
    assert asyncio.run(cases.open_case(None, cases.parse(message("approve")))) is None


# ---------- against Postgres (`make up` + `make db-schema`); skipped otherwise ----------

def in_rolled_back_transaction(test):
    async def run():
        try:
            conn = await psycopg.AsyncConnection.connect(ALERTS_DSN, autocommit=True,
                                                         connect_timeout=2)
        except psycopg.OperationalError:
            pytest.skip("Postgres not running")
        # Everything inside is undone at the end; open_case's own transaction becomes a
        # savepoint inside this one
        async with conn, conn.transaction(force_rollback=True):
            await test(conn)
    asyncio.run(run())


def test_flagged_payment_opens_one_case_with_one_audit_row():
    async def test(conn):
        msg = cases.parse(message("review"))
        case_id = await cases.open_case(conn, msg)
        assert case_id is not None
        assert await cases.open_case(conn, msg) is None   # delivered again: nothing new

        cur = await conn.execute("SELECT count(*), max(status) FROM cases WHERE txn_id = %s",
                                 (msg.txn_id,))
        assert await cur.fetchone() == (1, "open")
        cur = await conn.execute("SELECT id, at, actor, action, target, details, prev_hash, hash"
                                 " FROM audit_log WHERE target = %s", (f"case:{case_id}",))
        rows = await cur.fetchall()
        assert len(rows) == 1
        assert rows[0][2:5] == ("alert-svc", "case_opened", f"case:{case_id}")
        assert rows[0][5]["txn_id"] == msg.txn_id and rows[0][5]["decision"] == "review"
        assert first_broken(rows, start=rows[0][6]) is None

    in_rolled_back_transaction(test)


def test_degraded_flag_is_kept_on_the_case():
    async def test(conn):
        msg = cases.parse(message(degraded=True))
        await cases.open_case(conn, msg)
        cur = await conn.execute("SELECT degraded, reasons FROM cases WHERE txn_id = %s",
                                 (msg.txn_id,))
        assert await cur.fetchone() == (True, ["C1=72 (+2.32)"])

    in_rolled_back_transaction(test)
