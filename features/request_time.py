"""Features computed from a single request, with no history needed."""

import numpy as np


def log_amount(amount):
    """log(1 + amount). Squeezes the very wide range of amounts ($0.25 to $30,000)."""
    return np.log1p(amount)


def hour_of_day(transaction_dt):
    """Hour of day (0-23) from seconds since the dataset's start (TransactionDT)."""
    return (np.asarray(transaction_dt) // 3600) % 24