"""Tests for the retrain windows and the gate's choice of test days (no real data)."""

import pandas as pd
import pytest

from ml.eval.promotion_gate import GOLDEN_DAYS, golden_set, held_out_days, parse_days
from ml.train.retrain import CALIBRATION_DAYS, TEST_DAYS, TRAIN_DAYS, check_windows


class Meta:
    def __init__(self, info):
        self.meta = {"info": info}


def test_parse_days():
    assert parse_days("92-109") == (92, 109)
    with pytest.raises(ValueError):
        parse_days("120-110")


def test_gate_uses_the_candidates_own_test_days():
    assert held_out_days(Meta({"test_days": "169-182"})) == (169, 182)
    assert held_out_days(Meta({"test_days": "92-109"})) == (92, 109)
    assert held_out_days(Meta({})) == GOLDEN_DAYS            # older folders: the v1 golden days


def test_golden_set_keeps_whole_days_inclusive():
    df = pd.DataFrame({"TransactionDT": [91 * 86400 + 5, 92 * 86400, 109 * 86400 + 86399,
                                         110 * 86400]})
    assert golden_set(df, (92, 109)).index.tolist() == [1, 2]


def test_default_windows_respect_the_label_delay():
    train, cal, test = check_windows(TRAIN_DAYS, CALIBRATION_DAYS, TEST_DAYS)
    assert train[1] < cal[0] and test[0] > cal[1] + 14


def test_overlapping_or_too_early_windows_are_refused():
    with pytest.raises(ValueError):
        check_windows("95-140", "140-154", "169-182")      # overlap
    with pytest.raises(ValueError):
        check_windows("95-140", "141-154", "160-182")      # labels of day 154 not known yet