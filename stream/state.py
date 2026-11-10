"""Per-user state for the streaming feature job (week 4, step 1). Design: docs/spark.md

Both feature jobs use this: the Spark job (stream/spark_job.py) and the plain Python one
(stream/python_writer.py), so they can only differ in how they move data, never in the features.

The state keeps just enough of a user's past to give exactly what Manisha's reference,
features.history.history_for_one, gives with the user's FULL history:
  window   payments of the last 30 days: [t, amount, txn_id]. Every windowed feature (counts,
           24 h sum, 30 d mean and spread, zscore) only looks back 30 days
  n        how many payments in total                     -> n_prev_txns
  last_t   the latest payment's time                       -> secs_since_last
  seen     every value ever used, per NOVELTY_OF column    -> new_P_emaildomain, ...
It is a plain dict, stored as JSON. Kept small: the window is trimmed on every update.

tests/test_stream_state.py checks this against history_for_one on random histories.
"""

import json
import math
from datetime import UTC, datetime

from features.history import HISTORY_FEATURES, NOVELTY_OF, WINDOWS, history_for_one

WINDOW_SECONDS = WINDOWS["30d"]   # the longest look-back of any feature
UNKNOWN_PART = "NA"               # features/proxy_id.py writes this for a missing part

# What update() did with a payment
NEW, UNKNOWN_USER, DUPLICATE, LATE = "new", "unknown_user", "duplicate", "late"


def empty():
    return {"n": 0, "last_t": None, "window": [], "seen": {c: [] for c in NOVELTY_OF}}


def dumps(state):
    return json.dumps(state, separators=(",", ":"))


def loads(text):
    return json.loads(text) if text else empty()


def is_known(user_id):
    """Training gives no history to ids with a missing part (uid_known = False); so do we."""
    return UNKNOWN_PART not in str(user_id).split("|")


def to_epoch(ts):
    """ISO string (as ingest stores it) or datetime -> seconds since 1970, UTC."""
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)   # Python 3.11 reads the trailing Z
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.timestamp()


def _missing(v):
    return v is None or (isinstance(v, float) and math.isnan(v))


def _plain(v):
    """Missing -> None, so "never seen a value" and "seen a missing value" can be stored in JSON."""
    return None if _missing(v) else v


def no_history():
    return {name: float("nan") for name in HISTORY_FEATURES}


def features(state, t, amount, values):
    """The 12 history features for a payment at time t, given the state BEFORE it.
    Equal to history_for_one(full past, t, amount, values)."""
    # Only payments at or before t: one that arrived earlier but happened later isn't "past"
    past = [{"t": wt, "amount": wa, **{c: None for c in NOVELTY_OF}}
            for wt, wa, _ in state["window"] if wt <= t]
    out = history_for_one(past, t, amount, {c: None for c in NOVELTY_OF})
    # What the window alone can't know: everything older than 30 days
    n = state["n"]
    out["n_prev_txns"] = float(n)
    out["secs_since_last"] = float(t - state["last_t"]) if n else float("nan")
    for col in NOVELTY_OF:
        out[f"new_{col}"] = float(n > 0 and _plain(values.get(col)) not in state["seen"][col])
    return out


def update(state, txn_id, user_id, t, amount, values):
    """One payment, in arrival order. Returns (status, features or None, new state).

    status NEW:          features computed, state updated
    UNKNOWN_USER:        missing features, state untouched (training gives these no history)
    DUPLICATE:           already counted (the same txn_id is in the window): nothing changes.
                         Replays and redelivered messages send the same payment again
    LATE:                more than 30 days older than the user's latest payment: dropped, since
                         the window that would place it is gone
    """
    if not is_known(user_id):
        return UNKNOWN_USER, no_history(), state
    if any(wid == txn_id for _, _, wid in state["window"]):
        return DUPLICATE, None, state
    if state["last_t"] is not None and t < state["last_t"] - WINDOW_SECONDS:
        return LATE, None, state

    out = features(state, t, amount, values)

    seen = {c: list(state["seen"][c]) for c in NOVELTY_OF}
    for col in NOVELTY_OF:
        v = _plain(values.get(col))
        if v not in seen[col]:
            seen[col].append(v)
    last_t = t if state["last_t"] is None else max(state["last_t"], t)
    # Keep the window in time order (a slightly late payment slots in) and drop what has aged
    # out of every feature's look-back, relative to the newest payment seen
    window = sorted([*state["window"], [t, amount, txn_id]], key=lambda r: r[0])
    window = [r for r in window if r[0] >= last_t - WINDOW_SECONDS]
    return NEW, out, {"n": state["n"] + 1, "last_t": last_t, "window": window, "seen": seen}


def summary(state, now_t):
    """The per-user snapshot written to user_features."""
    day = [a for wt, a, _ in state["window"] if wt >= now_t - WINDOWS["24h"]]
    return {"n_txns": state["n"], "last_t": state["last_t"], "txn_count_24h": len(day),
            "amt_sum_24h": float(sum(day))}
