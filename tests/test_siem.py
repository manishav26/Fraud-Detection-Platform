"""The SIEM hook and the case system's routes (week 4, step 7; docs/siem.md).

The mock SIEM and the event format run anywhere. The forwarder and the case-system functions
need Postgres (`make up` + `make db-schema`) and are skipped without it; each runs in a
transaction that is rolled back.
"""

import asyncio
import importlib.util
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    # Several services have a package called `app`; load each module by path
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


siem = load("alert_siem", "services/alert-svc/app/siem.py")
cases = load("alert_cases_siem", "services/alert-svc/app/cases.py")
mock = load("siem_mock", "services/siem-mock/app/main.py")

TOKEN = "test-token"
AUTH = {"Authorization": f"Splunk {TOKEN}"}


def audit_row(row_id, action="case_opened"):
    at = datetime(2026, 11, 9, 10, 0, 0, 123456, tzinfo=UTC)
    return (row_id, at, "alert-svc", action, f"case:{row_id}", {"txn_id": f"t{row_id}"},
            "0" * 64, f"{row_id:064x}")


@pytest.fixture
def siem_app(monkeypatch):
    monkeypatch.setattr(mock, "TOKEN", TOKEN)
    mock._events.clear()
    return TestClient(mock.app)


# ---------- the event format ----------

def test_an_audit_row_becomes_one_hec_event_with_its_id_and_hash():
    event = siem.to_event(audit_row(7))
    assert event["sourcetype"] == "fraud:audit" and event["time"] == audit_row(7)[1].timestamp()
    e = event["event"]
    assert e["id"] == 7 and e["action"] == "case_opened" and e["hash"] == f"{7:064x}"
    assert e["at"] == "2026-11-09T10:00:00.123456+00:00"   # the same text the chain hashes


def test_a_batch_is_one_event_per_line_in_order():
    lines = siem.batch_body([audit_row(1), audit_row(2), audit_row(3)]).splitlines()
    assert [json.loads(line)["event"]["id"] for line in lines] == [1, 2, 3]


# ---------- the mock SIEM ----------

@pytest.mark.parametrize("header", [None, "Splunk wrong", f"Bearer {TOKEN}", "Splunk", ""])
def test_the_mock_refuses_a_missing_or_wrong_token(siem_app, header):
    headers = {"Authorization": header} if header is not None else {}
    r = siem_app.post("/services/collector/event", content=siem.batch_body([audit_row(1)]),
                      headers=headers)
    assert r.status_code == 401 and siem_app.get("/events").json() == []


def test_the_mock_refuses_everything_when_it_has_no_token(siem_app, monkeypatch):
    monkeypatch.setattr(mock, "TOKEN", "")
    r = siem_app.post("/services/collector/event", content="{}", headers={"Authorization": "Splunk "})
    assert r.status_code == 401


def test_the_mock_stores_a_batch_and_drops_ids_it_already_has(siem_app):
    body = siem.batch_body([audit_row(1), audit_row(2)])
    r = siem_app.post("/services/collector/event", content=body, headers=AUTH)
    assert r.status_code == 200 and r.json()["stored"] == 2
    r = siem_app.post("/services/collector/event",   # sent again after a crash: no duplicates
                      content=siem.batch_body([audit_row(2), audit_row(3)]), headers=AUTH)
    assert r.json()["stored"] == 1 and r.json()["duplicates"] == 1
    assert [e["event"]["id"] for e in siem_app.get("/events").json()] == [3, 2, 1]


def test_a_bad_batch_is_refused_whole(siem_app):
    body = siem.batch_body([audit_row(1)]) + "\n" + json.dumps({"event": {"no": "id"}})
    assert siem_app.post("/services/collector/event", content=body, headers=AUTH).status_code == 400
    assert siem_app.get("/events").json() == []   # the good first event wasn't kept either


def test_the_mock_keeps_only_the_newest(siem_app, monkeypatch):
    monkeypatch.setattr(mock, "MAX_EVENTS", 2)
    siem_app.post("/services/collector/event",
                  content=siem.batch_body([audit_row(i) for i in (1, 2, 3)]), headers=AUTH)
    assert [e["event"]["id"] for e in siem_app.get("/events").json()] == [3, 2]


def test_the_view_shows_events_and_escapes_them(siem_app):
    row = list(audit_row(5))
    row[4] = "<script>alert(1)</script>"
    siem_app.post("/services/collector/event", content=siem.batch_body([tuple(row)]), headers=AUTH)
    page = siem_app.get("/").text
    assert "case_opened" in page and "<script>alert" not in page and "&lt;script&gt;" in page


# ---------- the forwarder, against Postgres ----------

ALERTS_DSN = "postgresql://alerts:alerts_dev_only@localhost:5432/fraud"


async def connect():
    try:
        return await psycopg.AsyncConnection.connect(ALERTS_DSN, autocommit=True, connect_timeout=2)
    except psycopg.OperationalError:
        pytest.skip("Postgres not running")


def rolled_back(test):
    async def run():
        conn = await connect()
        async with conn, conn.transaction(force_rollback=True):
            try:
                await conn.execute("SELECT 1 FROM siem_cursor")
            except psycopg.errors.UndefinedTable:
                pytest.skip("no siem_cursor table: run make db-schema")
            await test(conn)
    asyncio.run(run())


def fake_siem(status=200):
    """An HTTP client whose SIEM answers `status` and remembers what it was sent."""
    received = []

    def answer(request):
        received.append(request)
        return httpx.Response(status, json={"code": 0 if status == 200 else 9})
    return httpx.AsyncClient(base_url="http://siem", transport=httpx.MockTransport(answer)), received


async def add_audit_rows(conn, n):
    from audit.chain import append_async
    for _ in range(n):
        await append_async(conn, "pytest", "test_event", f"pytest:{uuid.uuid4()}", {})


async def position(conn):
    return (await (await conn.execute("SELECT last_id FROM siem_cursor WHERE id = 1")).fetchone())[0]


def test_forwarder_sends_unsent_rows_in_order_then_moves_its_position():
    async def test(conn):
        last = (await (await conn.execute("SELECT coalesce(max(id), 0) FROM audit_log")).fetchone())[0]
        await conn.execute("UPDATE siem_cursor SET last_id = %s WHERE id = 1", (last,))
        await add_audit_rows(conn, 3)
        http, received = fake_siem()
        assert await siem.forward_once(conn, http, TOKEN) == 3
        ids = [json.loads(line)["event"]["id"] for line in received[0].content.decode().splitlines()]
        assert ids == sorted(ids) and ids[0] > last and received[0].headers["authorization"] == AUTH["Authorization"]
        assert await position(conn) == ids[-1]
        assert await siem.forward_once(conn, http, TOKEN) == 0   # caught up
        assert await siem.oldest_unsent(conn) is None
    rolled_back(test)


def test_forwarder_keeps_rows_when_the_siem_fails():
    async def test(conn):
        last = (await (await conn.execute("SELECT coalesce(max(id), 0) FROM audit_log")).fetchone())[0]
        await conn.execute("UPDATE siem_cursor SET last_id = %s WHERE id = 1", (last,))
        await add_audit_rows(conn, 2)
        http, _ = fake_siem(status=503)
        with pytest.raises(siem.SiemRefused):
            await siem.forward_once(conn, http, TOKEN)
        assert await position(conn) == last              # nothing counted as sent
        assert await siem.oldest_unsent(conn) is not None  # and the lag shows it
        http, _ = fake_siem()
        assert await siem.forward_once(conn, http, TOKEN) == 2   # sent once it answers again
    rolled_back(test)


def test_only_one_copy_sends_at_a_time():
    async def run():
        holder, other = await connect(), await connect()
        async with holder, other, holder.transaction(force_rollback=True):
            try:
                await holder.execute(siem.LOCK_CURSOR)   # the first copy, mid-batch
            except psycopg.errors.UndefinedTable:
                pytest.skip("no siem_cursor table: run make db-schema")
            http, received = fake_siem()
            assert await siem.forward_once(other, http, TOKEN) is None
            assert received == []
    asyncio.run(run())


# ---------- the case system's functions, against Postgres ----------

def scored(decision="review"):
    return cases.parse(json.dumps({
        "txn_id": f"pytest-{uuid.uuid4()}", "user_id": "u1", "amount": 50.0,
        "ts": "2017-12-10T10:00:00Z", "score": 0.8, "decision": decision,
        "model_version": "fraud-lgbm-v1", "reasons": ["C1=72 (+2.32)"]}).encode())


def cases_rolled_back(test):
    async def run():
        conn = await connect()
        async with conn, conn.transaction(force_rollback=True):
            await test(conn)
    asyncio.run(run())


def test_case_status_by_txn_hides_the_customer_and_amount():
    async def test(conn):
        msg = scored()
        await cases.open_case(conn, msg)
        status = await cases.get_status_by_txn(conn, msg.txn_id)
        assert status["status"] == "open" and status["decision"] == "review"
        assert "user_id" not in status and "amount" not in status
        assert await cases.get_status_by_txn(conn, "pytest-no-such-txn") is None
    cases_rolled_back(test)


def test_write_back_resolves_once_and_audits_the_ticket():
    async def test(conn):
        msg = scored("decline")
        case_id = await cases.open_case(conn, msg)
        result = await cases.resolve_by_txn(conn, msg.txn_id, "fraud", "confirmed by phone",
                                            "case-mgmt", external_ref="TICKET-42")
        assert result["status"] == "fraud" and result["resolved_by"] == "case-mgmt"
        with pytest.raises(cases.AlreadyResolved):
            await cases.resolve_by_txn(conn, msg.txn_id, "not_fraud", "", "case-mgmt")
        cur = await conn.execute("SELECT details FROM audit_log WHERE target = %s AND action ="
                                 " 'case_resolved'", (f"case:{case_id}",))
        assert (await cur.fetchone())[0]["external_ref"] == "TICKET-42"
        assert await cases.resolve_by_txn(conn, "pytest-no-such-txn", "fraud", "", "x") is None
    cases_rolled_back(test)


@pytest.mark.parametrize("header, allowed, ok", [
    ("case-system", cases.DECIDE_ROLES, True),
    ("analyst", cases.DECIDE_ROLES, False),     # analysts resolve by case id, not as the system
    ("case-system", cases.STATUS_ROLES, True),
    ("analyst", cases.STATUS_ROLES, True),
    ("client", cases.STATUS_ROLES, False),
    ("admin", cases.STATUS_ROLES, False),
    ("case-system", cases.RESOLVE_ROLES, False),  # and the system can't use the analyst routes
    ("case-system", cases.READ_ROLES, False),
])
def test_case_system_roles(header, allowed, ok):
    assert cases.has_role(header, allowed) is ok
