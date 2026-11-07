"""Drift detectors: has the data (or the model's behaviour) changed since training?

Two kinds of check:
- Batch, per feature: compare a window of new payments with the training data.
    psi()  Population Stability Index: how much the share of payments per bin has moved.
           Rule of thumb: < 0.1 stable, 0.1-0.2 moderate shift, > 0.2 major shift.
    ks()   Kolmogorov-Smirnov: largest gap between the two distributions (numeric only).
- Streaming, one value at a time:
    ADWIN  keeps a window of recent values and raises an alarm when the older part and the
           newer part of the window have clearly different means (Bifet & Gavalda, 2007).
           Fed with the model's score (no labels needed) or its error (once labels arrive).
"""

import math

import numpy as np
import pandas as pd
from scipy.stats import ks_2samp

EPS = 1e-4            # stands in for an empty bin, so PSI never divides by zero
PSI_MODERATE = 0.1
PSI_MAJOR = 0.2


def _psi_from_shares(ref_share, cur_share):
    ref_share = np.clip(ref_share, EPS, None)
    cur_share = np.clip(cur_share, EPS, None)
    return float(np.sum((cur_share - ref_share) * np.log(cur_share / ref_share)))


def psi(reference, current, bins=10):
    """PSI for a numeric feature. Bins are the reference's quantiles; missing is its own bin."""
    ref = pd.Series(reference, dtype=float)
    cur = pd.Series(current, dtype=float)
    edges = np.unique(np.nanquantile(ref, np.linspace(0, 1, bins + 1)))
    if len(edges) < 2:                       # constant (or all missing) in the reference
        edges = np.array([-np.inf, np.inf])
    edges[0], edges[-1] = -np.inf, np.inf    # new values outside the old range still count

    def shares(x):
        counts = np.histogram(x.dropna(), bins=edges)[0]
        counts = np.append(counts, x.isna().sum())          # last bin = missing
        return counts / max(len(x), 1)

    return _psi_from_shares(shares(ref), shares(cur))


def psi_categorical(reference, current):
    """PSI for a categorical feature: one bin per value seen in either window, plus missing."""
    ref = pd.Series(reference, dtype=object).fillna("<missing>")
    cur = pd.Series(current, dtype=object).fillna("<missing>")
    values = sorted(set(ref) | set(cur), key=str)
    ref_share = ref.value_counts(normalize=True).reindex(values, fill_value=0).to_numpy()
    cur_share = cur.value_counts(normalize=True).reindex(values, fill_value=0).to_numpy()
    return _psi_from_shares(ref_share, cur_share)


def ks(reference, current):
    """(KS statistic, p-value) for a numeric feature, ignoring missing values."""
    ref = pd.Series(reference, dtype=float).dropna()
    cur = pd.Series(current, dtype=float).dropna()
    if ref.empty or cur.empty:
        return float("nan"), float("nan")
    result = ks_2samp(ref, cur)
    return float(result.statistic), float(result.pvalue)


def level(psi_value):
    if psi_value > PSI_MAJOR:
        return "major"
    return "moderate" if psi_value > PSI_MODERATE else "stable"


def feature_drift(reference, current, features, categorical=()):
    """One row per feature: PSI, its level, and KS (numeric features only). Worst first."""
    rows = []
    for f in features:
        if f in categorical:
            value, stat, p = psi_categorical(reference[f], current[f]), float("nan"), float("nan")
        else:
            value = psi(reference[f], current[f])
            stat, p = ks(reference[f], current[f])
        rows.append({"feature": f, "PSI": value, "level": level(value),
                     "KS statistic": stat, "KS p-value": p})
    return pd.DataFrame(rows).sort_values("PSI", ascending=False, ignore_index=True)


class ADWIN:
    """ADaptive WINdowing drift detector for a stream of numbers (e.g. 0/1 errors, scores).

    Keeps the recent values. Every `check_every` values it tries each split of the window into
    an older part and a newer part; if their means differ by more than the Hoeffding bound
        eps = sqrt( ln(4 n / delta) / (2 m) ),  m = 1 / (1/n_old + 1/n_new)
    it reports drift and drops the older part, so the window again holds only the current
    behaviour. Smaller delta = fewer false alarms but slower to react.

    Simplification: values are kept as-is (up to max_window) instead of the paper's
    compressed buckets; that only changes memory use, not what counts as a change.
    """

    def __init__(self, delta=0.002, max_window=5000, check_every=32, min_part=30):
        self.delta = delta
        self.max_window = max_window
        self.check_every = check_every
        self.min_part = min_part
        self.window = []
        self.seen = 0
        self.drifts = []          # positions (1-based count of values seen) where drift fired

    @property
    def mean(self):
        return float(np.mean(self.window)) if self.window else float("nan")

    def update(self, value):
        """Add one value; returns True if drift was detected at this step."""
        self.window.append(float(value))
        self.seen += 1
        if len(self.window) > self.max_window:
            self.window.pop(0)
        if self.seen % self.check_every or len(self.window) < 2 * self.min_part:
            return False
        cut = self._find_cut()
        if cut is None:
            return False
        self.window = self.window[cut:]
        self.drifts.append(self.seen)
        return True

    def _find_cut(self):
        """Latest split where old and new means differ by more than eps, or None."""
        x = np.asarray(self.window)
        n = len(x)
        csum = np.cumsum(x)
        n_old = np.arange(self.min_part, n - self.min_part + 1)
        n_new = n - n_old
        mean_old = csum[n_old - 1] / n_old
        mean_new = (csum[-1] - csum[n_old - 1]) / n_new
        m = 1 / (1 / n_old + 1 / n_new)
        eps = np.sqrt(math.log(4 * n / self.delta) / (2 * m))
        hits = np.flatnonzero(np.abs(mean_old - mean_new) > eps)
        return int(n_old[hits[-1]]) if hits.size else None

class DDM:
    """Drift Detection Method (Gama et al., 2004) for a stream of 0/1 errors.

    Tracks the error rate p and its standard deviation s = sqrt(p (1 - p) / n). It remembers
    the lowest p + s seen so far (p_min, s_min). If p + s rises above p_min + 3 s_min, the
    error rate has clearly gone up: drift. (Above p_min + 2 s_min is only a warning.)
    After a drift it starts again from scratch, like ADWIN dropping its old window.
    Unlike ADWIN it only notices the error rate going UP, which is what matters for fraud.
    """

    def __init__(self, min_n=30, warning_level=2.0, drift_level=3.0):
        self.min_n = min_n
        self.warning_level = warning_level
        self.drift_level = drift_level
        self.seen = 0
        self.drifts = []          # positions (1-based count of values seen) where drift fired
        self.warning = False
        self._reset()

    def _reset(self):
        self.n = 0
        self.p = 0.0
        self.p_min = float("inf")
        self.s_min = float("inf")

    def update(self, error):
        """Add one value (1 = error, 0 = correct); returns True if drift was detected."""
        self.seen += 1
        self.n += 1
        self.p += (float(error) - self.p) / self.n
        s = math.sqrt(self.p * (1 - self.p) / self.n)
        if self.n < self.min_n:
            return False
        if self.p + s < self.p_min + self.s_min:
            self.p_min, self.s_min = self.p, s
        self.warning = self.p + s > self.p_min + self.warning_level * self.s_min
        if self.p + s > self.p_min + self.drift_level * self.s_min:
            self.drifts.append(self.seen)
            self.warning = False
            self._reset()
            return True
        return False