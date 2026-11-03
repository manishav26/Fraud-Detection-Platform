"""The audit chain (ST8). Tests against a real Postgres run inside a transaction that is rolled
back, so they never leave rows in a log that can't be cleaned up."""

import json
import os
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from audit.chain import GENESIS, append, first_broken, row_hash

T0 = datetime(2026, 11, 3, 10, 0, 0, 123456, tzinfo=UTC)
OWNER_DSN = os.getenv("AUDIT_DSN", "postgresql://fraud:fraud_dev_only@localhost:5432/fraud")
ALERTS_DSN = "postgresql://alerts:alerts_dev_only@localhost:5432/fraud"


def chain(n):
    rows, prev = [], GENESIS
    for i in range(n):
        at, details = T0 + timedelta(seconds=i), {"case": i, "score": 0.5 + i / 10}
        digest = row_hash(prev, at, "alert-svc", "case_opened", f"t{i}", details)
        rows.append((i + 1, at, "alert-svc", "case_opened", f"t{i}", details, prev, digest))
        prev = digest
    return rows


def test_intact_chain_passes():
    assert first_broken(chain(5)) is None
    assert first_broken([]) is None


@pytest.mark.parametrize("field, value", [(2, "someone-else"), (3, "case_closed"),
                                          (4, "t999"), (5, {"case": 2, "score": 0.1})])
def test_changed_row_is_found(field, value):
    rows = chain(5)
    row = list(rows[2])
    row[field] = value
    rows[2] = tuple(row)
    assert first_broken(rows) == (3, "its contents were changed after it was written")


def test_changed_time_is_found():
    rows = chain(5)
    rows[2] = (rows[2][0], rows[2][1] + timedelta(microseconds=1), *rows[2][2:])
    assert first_broken(rows)[0] == 3


def test_removed_row_is_found():
    rows = chain(5)
    del rows[2]
    assert first_broken(rows)[0] == 4   # the row after the gap no longer follows


def test_reordered_rows_are_found():
    rows = chain(5)
    rows[1], rows[2] = rows[2], rows[1]
    assert first_broken(rows) is not None


def test_same_time_in_another_timezone_gives_the_same_hash():
    other = T0.astimezone()   # the same moment in the laptop's own timezone
    assert row_hash(GENESIS, T0, "a", "b", "c", {}) == row_hash(GENESIS, other, "a", "b", "c", {})


# ---------- against Postgres (`make up` + `make db-schema`); skipped otherwise ----------

def connect(dsn):
    try:
        return psycopg.connect(dsn, connect_timeout=2)
    except psycopg.OperationalError:
        pytest.skip("Postgres not running")


@pytest.fixture
def owner():
    conn = connect(OWNER_DSN)
    yield conn
    conn.rollback()   # nothing a test wrote is kept
    conn.close()


def test_appended_rows_chain_and_survive_the_database_round_trip(owner):
    first = None
    for i in range(3):
        digest = append(owner, "pytest", "case_opened", f"t{i}", {"score": 0.123456789, "note": "é"})
        first = first or digest
    rows = owner.execute("SELECT id, at, actor, action, target, details, prev_hash, hash"
                         " FROM audit_log WHERE actor = 'pytest' ORDER BY id").fetchall()
    # Only our rows, linked to whatever was last before them: real rows may already be there
    assert rows[0][7] == first
    assert first_broken(rows, start=rows[0][6]) is None   # recomputed from what Postgres returns


@pytest.mark.parametrize("sql", ["UPDATE audit_log SET actor = 'x'", "DELETE FROM audit_log",
                                 "TRUNCATE audit_log"])
def test_trigger_refuses_changes_even_for_the_owner(owner, sql):
    append(owner, "pytest", "case_opened", "t1", {})
    # Order matters: the savepoint (inner) undoes the failed statement before the error is caught
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"), owner.transaction():
        owner.execute(sql)


@pytest.mark.parametrize("sql", ["DELETE FROM audit_log WHERE false",
                                 "UPDATE audit_log SET actor = 'x' WHERE false"])
def test_trigger_refuses_even_when_no_row_matches(owner, sql):
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"), owner.transaction():
        owner.execute(sql)


def test_alerts_login_cannot_change_the_log():
    conn = connect(ALERTS_DSN)
    try:
        append(conn, "pytest", "case_opened", "t1", {"x": 1})   # allowed
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute("UPDATE audit_log SET actor = 'x'")
    finally:
        conn.rollback()
        conn.close()


def test_details_are_stored_as_json(owner):
    append(owner, "pytest", "case_opened", "t1", {"reasons": ["C1=72 (+2.32)"]})
    row = owner.execute("SELECT details FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row[0] == json.loads(json.dumps({"reasons": ["C1=72 (+2.32)"]}))
