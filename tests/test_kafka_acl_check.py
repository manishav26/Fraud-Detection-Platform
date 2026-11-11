"""How the Kafka permission check judges each try (security/tests/kafka_acl_check.py)."""
from types import SimpleNamespace

from security.tests import kafka_acl_check as k


def fake_rpk(monkeypatch, returncode=0, out="", counts=(0, 0)):
    """rpk inside the pod answers with `returncode`/`out`; the topic holds counts[0] messages
    before the try and counts[1] after."""
    seen, counts = [], list(counts)
    monkeypatch.setattr(k.subprocess, "run", lambda cmd, **kw: seen.append(cmd) or
                        SimpleNamespace(returncode=returncode, stdout=out, stderr=""))
    monkeypatch.setattr(k, "messages", lambda topic: counts.pop(0))
    monkeypatch.setenv("REDPANDA_SCORING_PASSWORD", "x")
    return seen


def test_a_read_stops_at_the_current_end_so_an_empty_topic_does_not_wait():
    args, _ = k.read("model.events")
    assert args[-2:] == ["-o", ":end"] and "-n" in args


def test_an_allowed_read_of_an_empty_topic_counts_as_worked(monkeypatch):
    seen = fake_rpk(monkeypatch, returncode=0, out="")
    assert k.attempt("scoring", *k.read("model.events")) == "worked"
    assert ":end" in seen[0]


def test_a_refused_read_counts_as_refused(monkeypatch):
    fake_rpk(monkeypatch, returncode=1, out="TOPIC_AUTHORIZATION_FAILED: not authorized")
    assert k.attempt("scoring", *k.read("txn.raw")) == "refused"


def test_a_read_that_times_out_is_never_called_worked(monkeypatch):
    fake_rpk(monkeypatch, returncode=124, out="")
    assert k.attempt("scoring", *k.read("model.events")).startswith("unclear")


def test_a_write_is_judged_by_what_landed(monkeypatch):
    fake_rpk(monkeypatch, returncode=124, counts=(5, 6))
    assert k.attempt("scoring", *k.write("dlq.txn")) == "worked"
    fake_rpk(monkeypatch, returncode=124, counts=(5, 5))
    assert k.attempt("scoring", *k.write("model.events")) == "refused"
