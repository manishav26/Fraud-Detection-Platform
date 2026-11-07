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