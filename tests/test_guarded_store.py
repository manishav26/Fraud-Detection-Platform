import pytest

from store.guarded import CircuitBreaker, GuardedStore, HistoryUnavailable


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def breaker(clock):
    return CircuitBreaker(failures=3, open_seconds=5.0, clock=clock)


def open_it(b):
    for _ in range(3):
        assert b.allow()
        b.record_failure()


def test_stays_closed_until_enough_failures_in_a_row():
    b = breaker(Clock())
    b.record_failure()
    b.record_failure()
    b.record_success()   # resets the run of failures
    b.record_failure()
    b.record_failure()
    assert b.allow()


def test_opens_after_failures_and_refuses_calls():
    b = breaker(Clock())
    b.record_failure()
    b.record_failure()
    assert b.record_failure() is True    # reports the change once
    assert not b.allow()


def test_after_the_wait_only_one_trial_goes_through():
    clock = Clock()
    b = breaker(clock)
    open_it(b)
    clock.now += 5.0
    assert b.allow()
    assert not b.allow()   # the others keep waiting for the trial's result


def test_successful_trial_closes():
    clock = Clock()
    b = breaker(clock)
    open_it(b)
    clock.now += 5.0
    b.allow()
    assert b.record_success() is True
    assert b.allow() and b.allow()


def test_failed_trial_opens_for_another_wait():
    clock = Clock()
    b = breaker(clock)
    open_it(b)
    clock.now += 5.0
    b.allow()
    assert b.record_failure() is False   # already counted as open: no second log line
    clock.now += 4.9
    assert not b.allow()
    clock.now += 0.1
    assert b.allow()


def test_late_success_from_before_opening_does_not_close():
    b = breaker(Clock())
    open_it(b)
    assert b.record_success() is False
    assert not b.allow()


class FakeStore:
    def __init__(self):
        self.broken, self.calls, self.timeouts = False, 0, []

    def _call(self, timeout):
        self.calls += 1
        self.timeouts.append(timeout)
        if self.broken:
            raise TimeoutError("too slow")

    def past(self, user_id, ts, timeout=None):
        self._call(timeout)
        return ["row"]

    def save(self, txn, timeout=None):
        self._call(timeout)


def guarded(clock=None):
    store = FakeStore()
    return store, GuardedStore(store, breaker(clock or Clock()), read_timeout=0.05,
                               write_timeout=0.2)


def test_time_limits_are_passed_to_the_store():
    store, g = guarded()
    assert g.past("u", "t") == ["row"]
    g.save({})
    assert store.timeouts == [0.05, 0.2]


def test_failure_becomes_history_unavailable():
    store, g = guarded()
    store.broken = True
    with pytest.raises(HistoryUnavailable):
        g.past("u", "t")


def test_open_breaker_does_not_touch_the_store():
    store, g = guarded()
    store.broken = True
    for _ in range(3):
        with pytest.raises(HistoryUnavailable):
            g.past("u", "t")
    calls = store.calls
    with pytest.raises(HistoryUnavailable):
        g.past("u", "t")
    assert store.calls == calls   # no time wasted waiting on a broken Cassandra


def test_failed_write_names_the_payment_but_open_breaker_stays_quiet(caplog):
    store, g = guarded()
    store.broken = True
    for i in range(3):   # the third failure opens the breaker
        with pytest.raises(HistoryUnavailable):
            g.save({"txn_id": f"t{i}"})
    with pytest.raises(HistoryUnavailable):
        g.save({"txn_id": "refused"})
    assert "skipped history write for t0" in caplog.text
    assert "skipped history write for t2" in caplog.text
    assert "refused" not in caplog.text
    assert g.skipped_writes == 4


def test_recovers_and_counts_skipped_writes(caplog):
    clock = Clock()
    store, g = guarded(clock)
    store.broken = True
    for _ in range(3):
        with pytest.raises(HistoryUnavailable):
            g.save({})
    assert g.skipped_writes == 3

    store.broken = False
    clock.now += 5.0
    assert g.past("u", "t") == ["row"]
    assert g.skipped_writes == 0
    assert "3 writes were skipped" in caplog.text
