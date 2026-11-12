import pathlib
from types import SimpleNamespace

from ml.eval import promotion_gate as gate
from ml.eval.promotion_gate import check

CURRENT = {"PR-AUC": 0.48, "p95 ms": 2.0, "flagged share": 0.012}


def candidate(**changes):
    return {"PR-AUC": 0.50, "p95 ms": 2.2, "flagged share": 0.015, **changes}


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
    passed, rows = check(candidate(**{"PR-AUC": 0.60, "p95 ms": 2.5}), CURRENT)   # 1.25x
    assert not passed and failed(rows) == ["fast"]


def test_speed_is_judged_against_the_live_model_on_the_same_machine():
    """~11 ms for both models on a slow laptop is fine; it was the absolute 5 ms that failed it."""
    passed, rows = check(candidate(**{"p95 ms": 11.08}), {**CURRENT, "p95 ms": 11.09})
    assert passed and rows[1]["value"] == "11.08 ms vs 11.09 ms (1.00x)"


def test_flagging_more_than_analysts_can_review_is_rejected():
    passed, rows = check(candidate(**{"flagged share": 0.05}), CURRENT)
    assert not passed and failed(rows) == ["safe"]


def test_exactly_on_the_limits_passes():
    passed, _ = check(candidate(**{"PR-AUC": 0.49, "p95 ms": 2.4, "flagged share": 0.02}),
                      CURRENT)   # exactly +0.01, 1.2x, 2%
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


# Speed: both models timed together (measure_pair), so a busy moment on the machine can't fail `fast`

class FakeModel:
    def __init__(self, ms):
        self.ms = ms
        self.features = ["x"]
        self.meta = {"thresholds": {"review_threshold": 0.5}}

    def predict_rows(self, rows):
        import numpy as np
        return np.array([r["x"] for r in rows])


def golden_rows(n=200):
    import pandas as pd
    return pd.DataFrame({"x": [i / n for i in range(n)], gate.TARGET: [i % 2 for i in range(n)]})


def busy_timer(busy_calls):
    """Each model takes its own ms, plus 10 ms for every call made while the machine is busy
    (calls number busy_calls[0] to busy_calls[1], counted across both models)."""
    calls = [0]

    def timer(model, _row):
        calls[0] += 1
        return model.ms + (10 if busy_calls[0] <= calls[0] < busy_calls[1] else 0)
    return timer


def test_a_busy_moment_slows_both_models_alike_when_timed_together():
    same_speed_a, same_speed_b = FakeModel(10), FakeModel(10)
    golden = golden_rows()
    n = len(gate.timing_sample(golden))
    # busy for the first 15% of the calls after warm-up: one model after the other, that is
    # 30% of the first model's payments and none of the second's
    start = 2 * min(gate.WARM_UP, n)
    timer = busy_timer((start, start + int(0.3 * n)))
    cand, curr = gate.measure_pair(same_speed_a, same_speed_b, golden, timer=timer)
    assert cand["p95 ms"] == curr["p95 ms"] == 20
    assert check(cand, curr)[1][1]["passed"]

    # the same burst, timed one model after the other, as before 12 Nov: a false 2x slowdown
    timer = busy_timer((0, int(0.3 * n)))
    sequential = [float(gate.np.percentile([timer(m, None) for _ in range(n)], 95))
                  for m in (same_speed_a, same_speed_b)]
    assert sequential == [20, 10]


def test_timed_together_a_really_slower_candidate_is_still_caught():
    cand, curr = gate.measure_pair(FakeModel(13), FakeModel(10), golden_rows(), timer=busy_timer((0, 0)))
    assert (cand["p95 ms"], curr["p95 ms"]) == (13, 10)
    assert not check(cand, curr)[1][1]["passed"]


def test_both_models_are_timed_on_the_same_payments_in_alternating_order():
    seen = []
    a, b = FakeModel(1), FakeModel(1)
    gate.measure_pair(a, b, golden_rows(), sample=6, timer=lambda m, r: seen.append(("ab"[m is b], r["x"])) or 1)
    timed = seen[2 * 6:]   # after the warm-up (all 6 payments, both models)
    assert [m for m, _ in timed] == list("abbaabbaabba")
    assert [x for _, x in timed[0::2]] == [x for _, x in timed[1::2]]


def test_promote_and_the_gate_time_the_two_models_together():
    root = pathlib.Path(__file__).resolve().parents[1]
    assert "gate.measure_pair(candidate, current, golden)" in (root / "registry" / "promote.py").read_text()
    assert "measure_pair(candidate_model, current_model, golden)" in (
        root / "ml" / "eval" / "promotion_gate.py").read_text()
