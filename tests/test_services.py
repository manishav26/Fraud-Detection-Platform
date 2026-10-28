from pathlib import Path

import yaml
from app.main import Decision  # ingest-svc

from ml.model import FraudModel


def test_decision_thresholds_come_from_config():
    # scoring-svc decides with model.decide; the thresholds are config/decision.yaml
    thresholds = yaml.safe_load(Path("config/decision.yaml").read_text())
    model = FraudModel.__new__(FraudModel)
    model.meta = {"thresholds": thresholds}
    assert model.decide(0.1) == "approve"
    assert model.decide(thresholds["review_threshold"]) == "review"
    assert model.decide(0.5) == "review"
    assert model.decide(thresholds["decline_threshold"]) == "decline"


def test_ingest_passes_scoring_decision_through():
    d = Decision(txn_id="t1", score=0.8, decision="decline", model_version="v1")
    assert d.decision == "decline" and d.cold_start is False
