"""load/probe_attack.py: E6's attacker against the API, with a fake API."""

import numpy as np

from load import probe_attack
from store.probe import PROBE_FLAGS

ORIGINAL = {"txn_id": "1", "user_id": "9500|204.0|98.0", "amount": 100.0,
            "ts": "2017-12-10T10:00:00Z", "channel": "mobile",
            "attrs": {"P_emaildomain": "gmail.com", "DeviceType": "mobile"}}
POOLS = {f: ["a", "b", None] for f in probe_attack.FIELDS if f != "amount"}


def fake_api(answers):
    sent = []

    def send(req):
        sent.append(req)
        return answers[min(len(sent) - 1, len(answers) - 1)]
    return send, sent


def test_mutate_changes_one_field_and_keeps_half_the_amount():
    rng = np.random.default_rng(0)
    for _ in range(200):
        new = probe_attack.mutate(ORIGINAL, ORIGINAL, rng, POOLS)
        changed = [k for k in ("amount", "channel") if new[k] != ORIGINAL[k]]
        changed += [k for k in new["attrs"] if new["attrs"][k] != ORIGINAL["attrs"].get(k)]
        assert len(set(changed) - {"channel"}) <= 1
        assert 50.0 <= new["amount"] <= 100.0
    assert ORIGINAL["attrs"] == {"P_emaildomain": "gmail.com", "DeviceType": "mobile"}


def test_variants_have_new_ids_on_the_same_card_40_minutes_apart():
    send, sent = fake_api(["decline"])
    probe_attack.attack_card(send, ORIGINAL, np.random.default_rng(0), POOLS, 30, "p1")
    assert len(sent) == 31 and len({r["txn_id"] for r in sent}) == 31
    assert {r["user_id"] for r in sent} == {ORIGINAL["user_id"]}
    assert sent[0]["ts"] == "2017-12-10T10:00:01Z" and sent[-1]["ts"] == "2017-12-11T06:00:01Z"


def test_an_unflagged_payment_is_skipped():
    send, sent = fake_api(["approve"])
    assert probe_attack.attack_card(send, ORIGINAL, np.random.default_rng(0), POOLS, 30, "p") is None
    assert len(sent) == 1


def test_counts_approvals_after_five_flags():
    answers = ["decline", "review", "approve", "review", "review", "review", "approve", "approve"]
    send, _ = fake_api(answers)
    row = probe_attack.attack_card(send, ORIGINAL, np.random.default_rng(0), POOLS, 7, "p")
    assert row["flags"] == PROBE_FLAGS and row["approved"] == 3
    assert row["first_approval"] == 2 and row["approved_after"] == 2
