"""The consistency test (load/consistency_check.py, scripts/consistency_test.sh;
docs/results/consistency.md)."""
import pathlib
import re
import subprocess
from itertools import pairwise
from types import SimpleNamespace

import pytest
from cassandra import OperationTimedOut

from load import consistency_check as cc

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "consistency_test.sh"
STREAM_ROWS = 210_440


class FakeStore:
    """Scoring's store as the checker uses it: save(), and session.execute(select, ...) with the
    node that answered. Nodes in `behind` answer without any row, like a copy that missed it."""

    select = "SELECT"

    def __init__(self, route, behind=(), fail_writes=()):
        self.rows, self.route, self.behind, self.fail_writes = {}, route, set(behind), set(fail_writes)
        self.session = SimpleNamespace(execute=self.execute)

    def save(self, txn):
        if txn["user_id"] in self.fail_writes:
            raise OperationTimedOut("write timed out")
        self.rows[txn["user_id"]] = {"ts": txn["ts"], "amount": txn["amount"], "p_emaildomain": None,
                                     "device_type": None, "product_cd": None, "flagged": None}

    def execute(self, statement, params):
        assert statement == self.select
        user_id, before = params
        node = self.route(user_id)
        row = self.rows.get(user_id)
        rows = [] if node in self.behind or row is None or row["ts"] >= before else [row]
        return Result(rows, node)


class Result(list):
    def __init__(self, rows, node):
        super().__init__(rows)
        self.response_future = SimpleNamespace(coordinator_host=SimpleNamespace(address=node))


def by_number(user_id):   # spreads the test ids over three "nodes", like token-aware routing
    return f"127.0.0.{2 + int(user_id.rsplit('-', 1)[1]) % 3}"


NAMES = {"127.0.0.2": "cassandra-0", "127.0.0.3": "cassandra-1", "127.0.0.4": "cassandra-2"}


def test_tunnels_map_each_node_to_its_own_address():
    to_tunnel, names = cc.parse_tunnels("cassandra-0=10.42.0.5=127.0.0.2,cassandra-2=10.42.2.7=127.0.0.4")
    assert to_tunnel == {"10.42.0.5": "127.0.0.2", "10.42.2.7": "127.0.0.4"}
    assert names == {"127.0.0.2": "cassandra-0", "127.0.0.4": "cassandra-2"}
    translator = cc.TunnelTranslator(to_tunnel)
    assert translator.translate("10.42.2.7") == "127.0.0.4"
    assert translator.translate("10.42.9.9") == "10.42.9.9"   # unknown: left as it is
    assert cc.parse_tunnels("") == ({}, {})


def test_healthy_pairs_find_no_stale_read():
    tally = cc.check(FakeStore(by_number), "pairs", "r1", 30, rate=0, names=NAMES)
    s = cc.summary(tally, "pairs", "ONE", "r1", 30)
    assert (s["reads"], s["stale"], s["writes"], s["stale_percent"]) == (30, 0, 30, 0.0)
    assert s["reads_by_node"] == {"cassandra-0": 10, "cassandra-1": 10, "cassandra-2": 10}


def test_reads_from_a_node_behind_count_as_stale_and_name_it():
    store = FakeStore(by_number, behind={"127.0.0.4"})
    cc.check(store, "seed", "r2", 30, rate=0, names=NAMES)
    s = cc.summary(cc.check(store, "read", "r2", 30, rate=0, names=NAMES), "read", "ONE", "r2", 30)
    assert (s["reads"], s["stale"], s["stale_percent"]) == (30, 10, 33.33)
    assert s["stale_by_node"] == {"cassandra-2": 10}


def test_a_failed_write_is_counted_apart_never_as_stale():
    store = FakeStore(by_number, fail_writes={"ccr3-4"})
    s = cc.summary(cc.check(store, "pairs", "r3", 6, rate=0, names=NAMES), "pairs", "LOCAL_QUORUM", "r3", 6)
    assert (s["reads"], s["stale"], s["writes"]) == (5, 0, 5)
    assert s["failed"] == {"write: OperationTimedOut": 1}


def test_each_run_has_its_own_test_ids_after_every_real_payment():
    assert cc.run_ids("a", 2) == ["cca-0", "cca-1"]
    assert cc.payment("cca-1", 1)["ts"] > cc.BASE_TS
    assert cc.BASE_TS.year == 2031


def test_paced_spreads_the_work_over_time():
    started = []
    cc.paced(5, rate=50, work=lambda n: started.append(n))
    assert sorted(started) == [0, 1, 2, 3, 4]


def test_history_store_passes_a_translator_only_when_given(monkeypatch):
    from store import history

    seen = []

    class FakeCluster:
        def __init__(self, hosts, **kwargs):
            seen.append(kwargs)

        def connect(self, keyspace):
            return SimpleNamespace(prepare=lambda q: q)

    monkeypatch.setattr(history, "Cluster", FakeCluster)
    history.HistoryStore(hosts=["h"])
    translator = cc.TunnelTranslator({})
    history.HistoryStore(hosts=["h"], consistency="ONE", address_translator=translator)
    assert "address_translator" not in seen[0]
    assert seen[1]["address_translator"] is translator


def plan(*args, **env):
    return subprocess.run(["bash", str(SCRIPT), "plan", *args], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", **env}, check=False)   # tests check the code


RUNS = [("ONE", "healthy", "1"), ("LOCAL_QUORUM", "healthy", "1"), ("ONE", "healthy", "2"),
        ("LOCAL_QUORUM", "healthy", "2"), ("ONE", "behind", "1"), ("LOCAL_QUORUM", "behind", "1")]


def test_runs_use_their_own_rows_at_the_end_of_the_stream():
    phases = []
    for run in RUNS:
        out = plan(*run)
        assert out.returncode == 0, out.stderr
        for first, count in re.findall(r"rows (\d+) \+(\d+)", out.stdout):
            phases.append((int(first), int(first) + int(count)))
    assert len(phases) == 8
    phases.sort()
    assert phases[0][0] == 167_240 and phases[-1][1] <= STREAM_ROWS
    assert all(a[1] <= b[0] for a, b in pairwise(phases)), "two runs share rows"


@pytest.mark.parametrize("args, env", [(("ONE", "behind", "2"), {}), (("TWO", "healthy", "1"), {}),
                                       (("ONE", "healthy", "1"), {"RPS": "40"})])
def test_plan_refuses_bad_runs(args, env):
    assert plan(*args, **env).returncode != 0


def test_the_end_puts_everything_back_even_after_a_failure():
    text = SCRIPT.read_text()
    restore = text[text.index("restore() {"):text.index("trap restore EXIT")]
    for step in ("enablegossip", "enablebinary", "enablehandoff", "repair -full fraud",
                 "CASSANDRA_CONSISTENCY-", "minReplicas\\\":$min"):
        assert step in restore, step
    assert "trap restore EXIT" in text
    # the node is marked out before it is taken out, so a failure in between still puts it back
    assert text.index("NODE_OUT=1 REPAIR_DUE=1") < text.index("nt cassandra-2 disablebinary")
    assert text.index("HANDOFF_OFF=1") < text.index("nt cassandra-0 disablehandoff")


def test_a_zero_stale_count_needs_reads_from_the_node_behind():
    text = SCRIPT.read_text()
    assert text.count('seen=$(expect_reads') == 3   # an assignment, so a failed check stops the run
    assert "nodes.get('cassandra-2', 0) > 0" in text
    assert "ensure_tunnel_c2\n  phase back" in text


def tunnel_parts(spec):
    funcs = SCRIPT.parent.joinpath("k8s_tunnel.sh").read_text()
    body = "\n".join(re.findall(r"^(?:local_addr|local_port|remote_ports)\(\) \{.*?^\}", funcs, re.DOTALL | re.MULTILINE))
    out = subprocess.run(["bash", "-c", f'{body}\nt="$1"; echo "$(local_addr "$t") $(local_port "$t") $(remote_ports "$t")"',
                          "_", spec], capture_output=True, text=True, check=True)
    return out.stdout.split()


def test_tunnels_can_listen_on_their_own_loopback_address():
    assert tunnel_parts("service/cassandra=9042:9042") == ["localhost", "9042", "9042:9042"]
    assert tunnel_parts("pod/cassandra-1=127.0.0.3:9042:9042") == ["127.0.0.3", "9042", "9042:9042"]
