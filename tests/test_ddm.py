"""Tests for the DDM drift detector."""

import numpy as np

from ml.drift.detectors import DDM


def run(values):
    detector = DDM()
    return [i + 1 for i, v in enumerate(values) if detector.update(v)]


def test_ddm_fires_soon_after_the_error_rate_jumps():
    rng = np.random.default_rng(0)
    errors = np.r_[rng.random(3000) < 0.3, rng.random(3000) < 0.6]
    alarms = run(errors)
    assert alarms and 3000 < alarms[0] < 3500


def test_ddm_stays_quiet_on_a_stable_stream():
    rng = np.random.default_rng(1)
    assert run(rng.random(6000) < 0.3) == []


def test_ddm_ignores_the_error_rate_going_down():
    rng = np.random.default_rng(0)
    errors = np.r_[rng.random(3000) < 0.6, rng.random(3000) < 0.3]
    assert [a for a in run(errors) if a > 3000] == []


def test_ddm_starts_fresh_after_a_drift():
    detector = DDM()
    rng = np.random.default_rng(0)
    for v in np.r_[rng.random(2000) < 0.2, rng.random(2000) < 0.8]:
        if detector.update(v):
            break
    assert detector.n == 0 and detector.drifts