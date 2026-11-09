"""Probe detection (E6 defense D3, store/probe.py): the live rule = Manisha's simulated one."""

import numpy as np
import pytest

from ml.experiments.e6_evasion import PROBE_FLAGS as E6_FLAGS
from ml.experiments.e6_evasion import PROBE_WINDOW as E6_WINDOW
from ml.experiments.e6_evasion import probe_frozen
from store.probe import PROBE_FLAGS, PROBE_WINDOW, hold, recent_flags

DAY = 86_400


def past(*times, flagged=True):
    return [{"t": float(t), "flagged": flagged} for t in times]


def test_same_settings_as_e6():
    assert (PROBE_FLAGS, PROBE_WINDOW) == (E6_FLAGS, E6_WINDOW) == (5, DAY)


def test_the_sixth_payment_after_five_flags_is_held():
    flags = past(1, 2, 3, 4)
    assert hold("approve", flags, 10) == ("approve", False)            # 4 flags: not yet
    assert hold("approve", [*flags, *past(5)], 10) == ("review", True)  # 5 flags: held


def test_a_decline_stays_a_decline_and_a_review_a_review():
    five = past(1, 2, 3, 4, 5)
    assert hold("decline", five, 10) == ("decline", False)
    assert hold("review", five, 10) == ("review", False)


def test_the_window_rolls_and_its_edge_counts_like_e6():
    five = past(0, 100, 200, 300, 400)
    assert hold("approve", five, DAY) == ("review", True)          # the first is exactly 24 h old
    assert hold("approve", five, DAY + 1) == ("approve", False)    # it aged out: 4 left


def test_only_the_models_flags_count():
    held_before = past(1, 2, 3, 4, 5, flagged=False)   # saved as not flagged: holds or approvals
    assert recent_flags([*held_before, *past(6, 7, 8, 9)], 10) == 4
    assert hold("approve", [*held_before, *past(6, 7, 8, 9)], 10) == ("approve", False)


def test_rows_without_the_column_count_as_not_flagged():
    assert recent_flags([{"t": 1.0}, {"t": 2.0, "flagged": None}], 10) == 0


@pytest.mark.parametrize("seed", range(5))
def test_matches_e6_probe_frozen_on_random_cards(seed):
    """Random payments on a few cards, one a minute to one a few hours apart: the live rule
    (each payment against its card's earlier ones) holds exactly the payments E6 freezes."""
    rng = np.random.default_rng(seed)
    n = 400
    uids = rng.choice(["a", "b", "c"], n)
    times = np.cumsum(rng.integers(60, 4 * 3600, n))
    flagged = rng.random(n) < 0.4
    frozen = probe_frozen(uids, times, flagged)
    live = []
    for i in range(n):
        earlier = [{"t": float(times[j]), "flagged": bool(flagged[j])}
                   for j in range(i) if uids[j] == uids[i]]
        live.append(recent_flags(earlier, float(times[i])) >= PROBE_FLAGS)
    assert frozen.any() and (np.array(live) == frozen).all()
