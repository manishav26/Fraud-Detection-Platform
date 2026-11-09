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


def test_probe_flag_is_read_and_defaults_to_false():
    assert cases.parse(message(probe=True)).probe is True
    assert cases.parse(message()).probe is False   # messages from before step 7c

@pytest.mark.parametrize("bad", [b"not json", b"[]", message(ts="2017-12-10T10:00:00"),
                                 json.dumps({"txn_id": "t1"}).encode()])
def test_unreadable_message_is_none(bad):
    assert cases.parse(bad) is None


@pytest.mark.parametrize("header, allowed, ok", [
    ("analyst", cases.RESOLVE_ROLES, True),
    ("admin", cases.READ_ROLES, True),
    ("admin", cases.RESOLVE_ROLES, False),      # admin reads cases but doesn't resolve them
    ("client", cases.READ_ROLES, False),
    ("client, analyst", cases.RESOLVE_ROLES, True),
    ("", cases.READ_ROLES, False),
    (None, cases.READ_ROLES, False),            # no header: didn't come through Kong
    ("analysts", cases.READ_ROLES, False),      # a near-miss name isn't a role
])
def test_role_check_matches_the_roles_table(header, allowed, ok):
    assert cases.has_role(header, allowed) is ok


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


async def new_case(conn, decision="decline"):
    msg = cases.parse(message(decision))
    return await cases.open_case(conn, msg), msg


def test_list_and_read_open_cases():
    async def test(conn):
        case_id, msg = await new_case(conn)
        listed = await cases.list_cases(conn, "open", 200)
        assert case_id in [c["case_id"] for c in listed]
        case = await cases.get_case(conn, case_id)
        assert case["txn_id"] == msg.txn_id and case["status"] == "open"
        assert await cases.get_case(conn, -1) is None

    in_rolled_back_transaction(test)


def test_resolving_saves_who_and_writes_an_audit_row():
    async def test(conn):
        case_id, _ = await new_case(conn)
        case = await cases.resolve_case(conn, case_id, "not_fraud", "customer confirmed", "ana")
        assert case["status"] == "not_fraud" and case["resolved_by"] == "ana"
        assert case["resolved_at"] is not None and case["note"] == "customer confirmed"

        cur = await conn.execute("SELECT id, at, actor, action, target, details, prev_hash, hash"
                                 " FROM audit_log WHERE target = %s ORDER BY id",
                                 (f"case:{case_id}",))
        rows = await cur.fetchall()
        assert [r[3] for r in rows] == ["case_opened", "case_resolved"]
        assert rows[1][2] == "ana" and rows[1][5]["outcome"] == "not_fraud"
        assert first_broken(rows[1:], start=rows[1][6]) is None

    in_rolled_back_transaction(test)


def test_a_decision_is_never_overwritten():
    async def test(conn):
        case_id, _ = await new_case(conn)
        await cases.resolve_case(conn, case_id, "fraud", "", "ana")
        with pytest.raises(cases.AlreadyResolved):
            await cases.resolve_case(conn, case_id, "not_fraud", "", "ben")
        assert (await cases.get_case(conn, case_id))["resolved_by"] == "ana"
        cur = await conn.execute("SELECT count(*) FROM audit_log WHERE target = %s"
                                 " AND action = 'case_resolved'", (f"case:{case_id}",))
        assert (await cur.fetchone())[0] == 1   # the refused attempt wrote nothing

    in_rolled_back_transaction(test)


def test_resolving_a_missing_case_or_bad_outcome():
    async def test(conn):
        assert await cases.resolve_case(conn, -1, "fraud", "", "ana") is None
        case_id, _ = await new_case(conn)
        with pytest.raises(ValueError):
            await cases.resolve_case(conn, case_id, "maybe", "", "ana")

    in_rolled_back_transaction(test)


def test_degraded_flag_is_kept_on_the_case():
    async def test(conn):
        msg = cases.parse(message(degraded=True))
        await cases.open_case(conn, msg)
        cur = await conn.execute("SELECT degraded, reasons FROM cases WHERE txn_id = %s",
                                 (msg.txn_id,))
        assert await cur.fetchone() == (True, ["C1=72 (+2.32)"])

    in_rolled_back_transaction(test)
