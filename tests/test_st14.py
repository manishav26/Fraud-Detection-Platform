"""ST14 and the retrain rule it tests (security/tests/st14_retrain_guard.py,
ml/experiments/e3_retraining.py `DriftTriggered`)."""
import importlib.util
import pathlib
import random

from ml.experiments import e3_retraining as e3

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("st14", ROOT / "security" / "tests" / "st14_retrain_guard.py")
st14 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(st14)


def test_five_forced_alarms_give_one_retrain_and_four_logged_refusals():
    days, refused, messages, allowed_later = st14.run()
    assert days == [st14.DAY] and refused == 4
    assert messages == ["drift alarm waiting on day 120: retrain refused, cooldown until day 127"] * 4
    assert allowed_later   # a cooldown, not a lock


def test_the_weekly_cap_refuses_on_its_own_with_its_reason():
    policy = e3.DriftTriggered(cooldown=0, weekly_cap=1)
    policy.pending, policy.retrain_days = True, [100]
    assert not policy.wants_retrain(103)
    assert policy.refusals == [(103, "weekly cap of 1 retrain(s) reached")]
    assert policy.wants_retrain(107)   # a week later


def original_rule(policy, day):
    """DriftTriggered.wants_retrain before refusals were logged (Manisha's E3 version)."""
    if not policy.pending:
        return False
    if policy.retrain_days and day - policy.retrain_days[-1] < policy.cooldown:
        return False
    return sum(1 for d in policy.retrain_days if day - d < 7) < policy.weekly_cap


def test_logging_refusals_changed_no_decision():
    rng = random.Random(14)
    for _ in range(2000):
        policy = e3.DriftTriggered(cooldown=rng.choice([0, 3, 7]), weekly_cap=rng.choice([1, 2]))
        policy.pending = rng.random() < 0.8
        policy.retrain_days = sorted(rng.sample(range(90, 130), rng.randint(0, 4)))
        day = rng.randint(100, 140)
        assert policy.wants_retrain(day) == original_rule(policy, day)


def test_st14_would_fail_without_the_guard():
    class NoGuard(e3.DriftTriggered):
        def wants_retrain(self, day, **_):
            return self.pending
    days, refused, _, _ = st14.run(NoGuard)
    assert len(days) == st14.ALARMS and refused == 0   # every forced alarm became a retrain
