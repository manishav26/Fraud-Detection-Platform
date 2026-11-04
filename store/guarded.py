"""Keeps a slow or broken Cassandra from slowing down payments. Design: docs/scoring.md

GuardedStore puts a time limit on every call and a circuit breaker around them: after repeated
failures it stops trying for a few seconds, so each payment doesn't waste its time limit waiting.
"""

import logging
import os
import threading
import time

from prometheus_client import Counter, Gauge

log = logging.getLogger("scoring.history")

# Metrics replace the log lines as the record of breaker state (docs/metrics.md)
BREAKER_OPEN = Gauge("history_breaker_open", "1 while the breaker refuses calls to Cassandra")
OUTCOMES = Counter("history_calls_total", "Calls to Cassandra by result",
                   ["op", "result"])   # result: ok, failed, refused (breaker open)
SKIPPED_WRITES = Counter("history_skipped_writes_total",
                         "Payments missing from history because their write failed or was refused")

READ_TIMEOUT = float(os.getenv("CASSANDRA_READ_TIMEOUT_MS", "50")) / 1000
# Longer than reads: a write that times out may still land, and writing it again is safe, but a
# skipped write leaves a gap in the user's history
WRITE_TIMEOUT = float(os.getenv("CASSANDRA_WRITE_TIMEOUT_MS", "200")) / 1000


class HistoryUnavailable(Exception):
    """Cassandra didn't answer in time, or the breaker is open."""


class CircuitBreaker:
    """Closed: calls go through. After `failures` in a row it opens: calls are refused for
    `open_seconds`. Then one trial call goes through: success closes it, failure opens it again.
    Requests run in parallel threads, so every change happens under a lock.
    """

    def __init__(self, failures=5, open_seconds=5.0, clock=time.monotonic):
        self.failures, self.open_seconds, self.clock = failures, open_seconds, clock
        self.lock = threading.Lock()
        self.count = 0
        self.opened_at = None   # None = closed
        self.trial = False      # the one trial call is running

    def allow(self):
        with self.lock:
            if self.opened_at is None:
                return True
            if self.trial or self.clock() - self.opened_at < self.open_seconds:
                return False
            self.trial = True
            return True

    def record_success(self):
        """Returns True if this success closed an open breaker."""
        with self.lock:
            if self.opened_at is None:
                self.count = 0
                return False
            if not self.trial:
                return False   # a call that started before the breaker opened: proves nothing
            self.opened_at, self.trial, self.count = None, False, 0
            return True

    def record_failure(self):
        """Returns True if this failure opened a closed breaker."""
        with self.lock:
            if self.opened_at is not None:
                if self.trial:
                    self.opened_at, self.trial = self.clock(), False
                return False
            self.count += 1
            if self.count < self.failures:
                return False
            self.opened_at = self.clock()
            return True


class GuardedStore:
    def __init__(self, store, breaker=None, read_timeout=READ_TIMEOUT, write_timeout=WRITE_TIMEOUT):
        self.store, self.read_timeout, self.write_timeout = store, read_timeout, write_timeout
        self.breaker = breaker or CircuitBreaker()
        self.skipped_writes = 0   # since the breaker last opened; these are gaps in history

    def _guarded(self, op, call):
        if not self.breaker.allow():
            OUTCOMES.labels(op, "refused").inc()
            raise HistoryUnavailable("breaker open")
        try:
            result = call()
        except Exception as e:
            OUTCOMES.labels(op, "failed").inc()
            # One line per change of state, not per payment: an outage would flood the log
            if self.breaker.record_failure():
                BREAKER_OPEN.set(1)
                log.error("Cassandra failing: scoring without history (%s)", e)
            raise HistoryUnavailable(str(e)) from e
        OUTCOMES.labels(op, "ok").inc()
        if self.breaker.record_success():
            BREAKER_OPEN.set(0)
            log.warning("Cassandra back: history in use again; %d writes were skipped",
                        self.skipped_writes)
            self.skipped_writes = 0
        return result

    def past(self, user_id, ts):
        return self._guarded("read", lambda: self.store.past(user_id, ts,
                                                             timeout=self.read_timeout))

    def save(self, txn):
        try:
            self._guarded("write", lambda: self.store.save(txn, timeout=self.write_timeout))
        except HistoryUnavailable as e:
            self.skipped_writes += 1
            SKIPPED_WRITES.inc()
            # A real failure, not the breaker refusing: name the payment, so a gap can be traced.
            # While the breaker is open the count above is enough, or an outage would flood the log.
            if e.__cause__ is not None:
                log.warning("skipped history write for %s: %s", txn.get("txn_id"), e)
            raise

    def close(self):
        self.store.close()
