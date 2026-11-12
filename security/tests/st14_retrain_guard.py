"""ST14 (threat 12): an attacker forcing drift alarms must not force retrains.

    make st14

Offline (agreed with Manisha, 12 Nov): the retrain rule is the drift strategy's own
(`DriftTriggered` in ml/experiments/e3_retraining.py: a 7-day cooldown, at most 1 retrain a
week); nothing on the cluster decides retrains yet (a live drift job is future work). Its
detector is replaced by one that alarms on every update, so 5 alarms arrive in one simulated day
exactly as an attacker would force them; everything else is the real policy.

Pass (security/threat-model.md): 5 alarms in one day -> 1 retrain; the other 4 refused, each
logged with its reason; and after the cooldown a new alarm is allowed again (a cooldown, not a lock).
"""

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ml.experiments import e3_retraining as e3

ALARMS, DAY = 5, 120


class AlwaysAlarms:
    """Stands in for ADWIN: every update is a drift alarm (the attacker's best case)."""

    def update(self, _value):
        return True


class Captured(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def run(policy_class=None):
    """The ST14 scenario. Returns (retrain days, refused alarms, logged messages, retrain allowed
    after the cooldown)."""
    original, e3.ADWIN = e3.ADWIN, AlwaysAlarms
    handler = Captured()
    e3.log.addHandler(handler)
    e3.log.setLevel(logging.INFO)
    try:
        policy = (policy_class or e3.DriftTriggered)()
        refused = 0
        for _ in range(ALARMS):                    # 5 forced alarms on the same day
            policy.observe(DAY, [1])
            if policy.wants_retrain(DAY):
                policy.retrained(DAY)
            else:
                refused += 1
        later = DAY + policy.cooldown              # the first day the cooldown allows
        policy.observe(later, [1])
        allowed_later = policy.wants_retrain(later)
        return list(policy.retrain_days), refused, handler.messages, allowed_later
    finally:
        e3.ADWIN = original
        e3.log.removeHandler(handler)


def main():
    days, refused, messages, allowed_later = run()
    print(f"{ALARMS} drift alarms forced on day {DAY}: {len(days)} retrain(s) on day(s) {days}, "
          f"{refused} refused")
    for m in messages:
        print(f"  logged: {m}")
    print(f"a new alarm on day {DAY + e3.COOLDOWN_DAYS}, after the cooldown: "
          f"{'retrain allowed' if allowed_later else 'still refused'}")
    ok = (len(days) == 1 and refused == ALARMS - 1 and len(messages) == ALARMS - 1
          and all("refused" in m and "cooldown" in m for m in messages) and allowed_later)
    if not ok:
        sys.exit("ST14 FAILED")
    print("ST14 PASSED: forced alarms gave 1 retrain, the rest were refused with their reason")


if __name__ == "__main__":
    main()
