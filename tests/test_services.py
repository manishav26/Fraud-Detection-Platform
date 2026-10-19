from app.main import decide  # ingest-svc


def test_decide_thresholds():
    assert decide(0.1) == "allow"
    assert decide(0.5) == "review"
    assert decide(0.9) == "block"
