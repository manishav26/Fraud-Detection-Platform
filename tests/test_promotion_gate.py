import pathlib
from types import SimpleNamespace

from ml.eval import promotion_gate as gate
from ml.eval.promotion_gate import check

CURRENT = {"PR-AUC": 0.48, "p95 ms": 2.0, "flagged share": 0.012}


def candidate(**changes):
    return {"PR-AUC": 0.50, "p95 ms": 3.0, "flagged share": 0.015, **changes}


def failed(rows):
    return [r["check"] for r in rows if not r["passed"]]


def test_better_faster_and_within_capacity_passes():
    passed, rows = check(candidate(), CURRENT)
    assert passed and failed(rows) == []


def test_gain_within_seed_noise_is_not_enough():
    passed, rows = check(candidate(**{"PR-AUC": 0.485}), CURRENT)
    assert not passed and failed(rows) == ["better"]


def test_worse_model_is_rejected():
    passed, rows = check(candidate(**{"PR-AUC": 0.40}), CURRENT)
    assert not passed and "better" in failed(rows)


def test_too_slow_is_rejected_even_if_better():
    passed, rows = check(candidate(**{"PR-AUC": 0.60, "p95 ms": 68.0}), CURRENT)
    assert not passed and failed(rows) == ["fast"]


def test_flagging_more_than_analysts_can_review_is_rejected():
    passed, rows = check(candidate(**{"flagged share": 0.05}), CURRENT)
    assert not passed and failed(rows) == ["safe"]


def test_exactly_on_the_limits_passes():
    passed, _ = check(candidate(**{"PR-AUC": 0.49, "p95 ms": 5.0, "flagged share": 0.02}),
                      CURRENT)
    assert passed

# The test days come from the candidate's own meta.json, so the gate checks both models never saw them

def model_with(train=None, calibration=None):
    info = {k: v for k, v in (("train_days", train), ("calibration_days", calibration)) if v}
    return SimpleNamespace(meta={"info": info})


V1 = model_with("1-73", "74-91")


def test_v2_on_its_own_test_days_is_judged_fairly():
    row = gate.unseen((169, 182), model_with("95-140", "141-154"), V1)
    assert row["passed"] and row["check"] == "unseen"


def test_a_candidate_naming_days_it_trained_on_is_refused():
    row = gate.unseen((120, 130), model_with("95-140", "141-154"), V1)
    assert not row["passed"] and "candidate trained on 95-140" in row["value"]


def test_test_days_the_current_model_was_calibrated_on_are_refused_too():
    row = gate.unseen((80, 100), model_with("95-140", "141-154"), V1)
    assert not row["passed"] and "current calibrated on 74-91" in row["value"]


def test_a_candidate_that_hides_its_training_days_is_refused():
    row = gate.unseen((169, 182), model_with(), V1)
    assert not row["passed"] and "doesn't record" in row["value"]


def test_promote_judges_the_candidate_on_its_own_unseen_days():
    text = (pathlib.Path(__file__).resolve().parents[1] / "registry" / "promote.py").read_text()
    assert "gate.golden_set(gate.load_data(), days)" in text and "gate.held_out_days(candidate)" in text
    assert "rows.append(gate.unseen(days, candidate, current))" in text
